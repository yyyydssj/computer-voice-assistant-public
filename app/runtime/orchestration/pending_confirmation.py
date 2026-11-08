"""P10 · T1 批次 3：语音"独立确认轮"的**路由层状态**（纯离线，不读环境/时钟以外的东西）。

为什么放在路由层：语音链路上**所有** ASR final 都必须经过 `ConversationManager` 的路由器
（`input_seam.submit()`），所以"确认"这句话也一定会被路由一次。若不加状态，它会变成一次普通
聊天（触发 Agent / 计费）并被念出来。这里把"待确认"做成路由器的一份**一次性**状态：

```text
上一轮：任务路由 -> 会话播报计划 -> router.arm(任务交接)   （20 s 窗口，不执行）
                       ↑ 真机教训（2026-10-13，#15）：窗口**从播报结束起算**（`rearm()`，
                         由 `auto_voice` 在本轮播报完成后调用），否则计划一念完就超时。
下一轮：ASR final
   ├─ 严格等于「确认」      -> 返回**同一个任务**的交接（provider=local-pending-confirmation，一次性）
   ├─ 重复句/回声(source=asr)-> **不作废**待确认，返回同任务的重复标记
   │                           （provider=local-pending-confirmation-repeat，会话只重问一次、不重新出计划）
   ├─ 换了另一个目标        -> 清除待确认，并按新任务下传（会话据此播报"已取消"再出新计划）
   ├─ 本批不支持的任务句    -> 清除待确认（按"换目标"），把 unsupported 交接下传**如实拒绝**（#12）
   ├─ 说了别的              -> **不作废**待确认，只记含糊（会话最多重问一次）
   └─ 超过窗口              -> 清除待确认，按超时处理
```
没有待确认时，本路由器与 `TaskFirstRouter` 行为**逐字节一致**（本地任务句式优先 → 未命中交给 fallback）。

真机教训（2026-10-13）：确认轮里 ASR 会把整句任务句（如「小饼打开计算器。」）再识别一遍。旧口径把它
当成"换了新任务" ⇒ 作废计划 + 播「已取消」+ 重新出计划 ⇒ 用户永远等不到「确认」。重复判定**借公开实现**
（先抄后改，出处记在函数注释里）：

- 撞上"我们自己正在等的那句话" ⇒ 按回声丢弃 —— AgenticSeek `sources/echo_filter.py`（把 STT 文本与上一条
  TTS 文本做连续 `min_consecutive_words=3` 片段的**双向**重叠比对；中文没有词边界，这里按**字符**切）；
- 同一批文本完全相同即去重 —— docker-talkies `src/talkies/models/sherpa.py` 的 `text == self._last_partial`；
- "重问有限次、每次各自计时" —— Mycroft/OVOS `MycroftSkill.get_response(num_retries)` + `_wait_response`
  （会话侧对应 `voice_task_turn.REASON_REPEAT` 与本模块的 `rearm()`）。
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus
from app.runtime.orchestration.app_launch import (
    ALLOWED_INTENTS,
    AppLaunchError,
    normalize_application_name,
    resolve_application_id,
)
from app.runtime.orchestration.local_task_router import (
    ROUTE_UNSUPPORTED,
    STRIP_CHARACTERS,
    RouterLike,
    downgrade_question_unsupported,
)
from app.runtime.orchestration.task_dispatch import is_confirmation
from app.runtime.orchestration.task_flow import TASK_ROUTES

PENDING_CONFIRMATION_PROVIDER = "local-pending-confirmation"
#: "重复句/回声"标记：路由仍是 `task`，但这个 provider 表示"待确认计划**仍然有效**、本轮别出新计划"。
PENDING_CONFIRMATION_REPEAT_PROVIDER = "local-pending-confirmation-repeat"
DEFAULT_CONFIRM_WINDOW_SECONDS = 20.0

#: 重复判定的连续字符数（借 AgenticSeek `echo_filter.py` 的 `min_consecutive_words=3`；
#: 中文没有词边界，因此按字符切。长度不足 3 的一律不算重复 ⇒「打开」这种 2 字公共前缀不会误判）。
REPEAT_OVERLAP_CHARACTERS = 3

DROPPED_TIMEOUT = "timeout"
DROPPED_CANCELLED = "cancelled"
DROPPED_REPLACED = "replaced"

#: 明确的"取消/算了"词：出现即立刻作废（这是用户本人的反悔，不该再等）
CANCEL_WORDS = ("取消", "算了", "不用", "不要", "停止", "退出", "别打开", "不了")


def is_pending_confirmation(handoff: object) -> bool:
    """判断一次交接是不是"待确认任务被确认"的结果（靠 `router.provider` 标记）。"""

    return (
        isinstance(handoff, RouteHandoff)
        and handoff.router.provider == PENDING_CONFIRMATION_PROVIDER
    )


def is_repeat_confirmation(handoff: object) -> bool:
    """判断一次交接是不是"重复句/回声"（计划仍待确认，本轮**不得**重新出计划）。"""

    return (
        isinstance(handoff, RouteHandoff)
        and handoff.router.provider == PENDING_CONFIRMATION_REPEAT_PROVIDER
    )


def _is_cancel(text: object) -> bool:
    """是否是明确的"取消/算了"（只对用户本人的反悔立刻生效）。"""

    if type(text) is not str:
        return False
    value = unicodedata.normalize("NFKC", text).strip()
    return any(word in value for word in CANCEL_WORDS)


def _is_asr(request: Any) -> bool:
    """这一轮是不是**语音**输入（文本入口保持旧口径，不受重复判定影响）。"""

    return getattr(getattr(request, "trace", None), "source", None) == "asr"


def _normalized_text(text: object) -> str:
    """归一化一段文本用于"重复/回声"比较：NFKC + casefold + 去标点与空白。

    对应 AgenticSeek `echo_filter.py` 里"两边都归一化后再比对"的做法；中文没有词边界，
    所以按**字符**切（不按词切）。
    """

    if type(text) is not str:
        return ""
    value = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in value if char not in STRIP_CHARACTERS and not char.isspace())


def _has_overlapping_run(
    first: str, second: str, *, size: int = REPEAT_OVERLAP_CHARACTERS
) -> bool:
    """两段归一化文本是否存在长度 `size` 的公共连续片段（与 AgenticSeek 一样**双向**看）。

    已知边界（如实记录）：`min=3` 只挡住 2 字公共前缀；`打开计算器` 与 `打开计算机` 会因为
    `打开计` 被判成重复。判成重复只会"保持待确认 + 重问一次"（**绝不执行**），属 fail-safe 方向。
    """

    if len(first) < size or len(second) < size:
        return False
    return any(
        first[index : index + size] in second for index in range(len(first) - size + 1)
    ) or any(
        second[index : index + size] in first for index in range(len(second) - size + 1)
    )


def _application_name(handoff: RouteHandoff) -> str | None:
    value = getattr(handoff.slots.get("application_name"), "value", None)
    return value if type(value) is str and value.strip() else None


def _same_target(first: RouteHandoff, second: RouteHandoff) -> bool:
    """两个任务交接是否指向**同一个白名单应用**（归一化名相等，或别名解析到同一个 id）。"""

    name_a = _application_name(first)
    name_b = _application_name(second)
    if name_a is None or name_b is None:
        return False
    try:
        if normalize_application_name(name_a) == normalize_application_name(name_b):
            return True
    except Exception:  # noqa: BLE001 - 归一化失败不得把"重复"误判成"换了目标"
        return False
    try:
        return resolve_application_id(name_a) == resolve_application_id(name_b)
    except AppLaunchError:
        return False
    except Exception:  # noqa: BLE001 - 解析失败一律按"不同目标"处理（退回旧行为）
        return False


def is_repeat_of_pending(pending_handoff: RouteHandoff, new_handoff: RouteHandoff) -> bool:
    """新交接是不是"待确认那句又听了一遍"：同目标**或**与待确认原文有连续片段重叠（回声）。"""

    if _same_target(pending_handoff, new_handoff):
        return True
    return _has_overlapping_run(
        _normalized_text(pending_handoff.trace.raw_text),
        _normalized_text(new_handoff.trace.raw_text),
    )


@dataclass(frozen=True, slots=True)
class PendingTask:
    """一份等待确认的任务交接（含窗口期限，纯本地事实）。"""

    handoff: RouteHandoff
    armed_at: float
    deadline: float


class PendingConfirmationRouter:
    """`TaskFirstRouter` 的确认态扩展：arm 后只吃一次「确认」。"""

    def __init__(
        self,
        *,
        first: RouterLike,
        fallback: RouterLike,
        window_seconds: float = DEFAULT_CONFIRM_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(getattr(first, "route", None)):
            raise TypeError("first 必须有 route()")
        if not callable(getattr(fallback, "route", None)):
            raise TypeError("fallback 必须有 route()")
        if type(window_seconds) not in (int, float) or window_seconds <= 0:
            raise ValueError("window_seconds 必须是正数")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self._first = first
        self._fallback = fallback
        self._window_seconds = float(window_seconds)
        self._clock = clock
        self._pending: PendingTask | None = None
        self._dropped: str | None = None
        self._vague = False
        self._last_handoff: RouteHandoff | None = None

    # ---- 供会话使用 ---------------------------------------------------------

    @property
    def pending(self) -> PendingTask | None:
        return self._pending

    @property
    def last_handoff(self) -> RouteHandoff | None:
        """最近一次 `route()` 的结果（会话据此判断本轮是不是任务路由）。"""

        return self._last_handoff

    def arm(self, handoff: RouteHandoff) -> PendingTask:
        """把一条任务交放置为"待确认"；只接受 task/dangerous 的合法任务交接。"""

        if not isinstance(handoff, RouteHandoff):
            raise TypeError("handoff 必须是 RouteHandoff")
        if handoff.route not in TASK_ROUTES:
            raise ValueError("只有 task/dangerous 路由可以进入待确认")
        if handoff.intent not in ALLOWED_INTENTS:
            raise ValueError("本批只支持 open_application 进入待确认")
        if handoff.required_confirmation != "defer_to_safety":
            raise ValueError("任务交接必须交由本地安全层确认")
        now = self._clock()
        self._pending = PendingTask(
            handoff=handoff, armed_at=now, deadline=now + self._window_seconds
        )
        self._dropped = None
        return self._pending

    def clear(self) -> None:
        self._pending = None

    def rearm(self) -> PendingTask | None:
        """把待确认窗口**从现在**重新计时（会话重问一次后调用）；没有待确认时返回 ``None``。

        借 Mycroft/OVOS `get_response` 的口径：每一次"问"都有**自己**的等待时间，
        所以重问之后不应继续吃上一轮剩下的秒数。只在会话真的重问一次时调用；
        静默忽略的重复句**不**延长窗口（避免 ASR 卡死时计划无限期挂着）。
        """

        pending = self._pending
        if pending is None:
            return None
        now = self._clock()
        self._pending = PendingTask(
            handoff=pending.handoff, armed_at=now, deadline=now + self._window_seconds
        )
        return self._pending

    def take_dropped(self) -> str | None:
        """消费"待确认被丢弃"的事实（`timeout`/`cancelled`/`replaced`），只报一次。"""

        value = self._dropped
        self._dropped = None
        return value

    def take_vague(self) -> bool:
        """消费"听到含糊回答、但**仍保持待确认**"的事实（供会话重问一次），只报一次。"""

        value = self._vague
        self._vague = False
        return value

    # ---- 路由 ---------------------------------------------------------------

    def route(self, request: Any) -> RouteHandoff:
        pending = self._pending
        if pending is not None:
            raw_text = getattr(getattr(request, "trace", None), "raw_text", None)
            if self._clock() > pending.deadline:
                self._pending = None
                self._dropped = DROPPED_TIMEOUT
            elif is_confirmation(raw_text):
                self._pending = None
                handoff = self._confirmed_handoff(pending.handoff, request.trace)
                self._last_handoff = handoff
                return handoff
            elif _is_cancel(raw_text):
                # 用户本人的反悔 ⇒ 立刻作废（不再等窗口）
                self._pending = None
                self._dropped = DROPPED_CANCELLED
            else:
                handoff = self._first.route(request)
                if handoff is not None and handoff.route == ROUTE_UNSUPPORTED:
                    # P10 · T1 #12：本批**明确不支持**的任务句（关闭/删除等）不是"含糊回答" ——
                    # 用户意图已经变了，旧计划按"换目标"作废，并把如实拒绝的交接下传。
                    self._pending = None
                    self._dropped = DROPPED_REPLACED
                    self._last_handoff = handoff
                    return handoff
                if handoff is not None and handoff.route in TASK_ROUTES:
                    if _is_asr(request) and is_repeat_of_pending(pending.handoff, handoff):
                        # 真机缺陷（2026-10-13）：确认轮里 ASR 又把整句任务句识别出来。
                        # 借 AgenticSeek echo_filter / docker-talkies：撞上"我们自己正在等的那句话"
                        # 就按回声丢弃这一轮 —— **不作废计划**、不重新出计划，交给会话最多重问一次。
                        self._last_handoff = self._repeat_handoff(pending.handoff, request.trace)
                        return self._last_handoff
                    # 换了一个新任务句 ⇒ 旧计划作废，交给会话"先取消再准备"
                    self._pending = None
                    self._dropped = DROPPED_REPLACED
                    self._last_handoff = handoff
                    return handoff
                # 真机教训（2026-10-13）：用户说「确认执行」还没被识别完，旧实现就把计划作废了。
                # 现在**含糊回答不作废计划**：窗口内继续等「确认」，本轮该聊什么就聊什么。
                # 2026-10-23：fallback（Provider）把疑问句判成"不支持"时回落聊天，见
                # `downgrade_question_unsupported`。
                self._vague = True
                handoff = downgrade_question_unsupported(self._fallback.route(request), request)
                self._last_handoff = handoff
                return handoff
        handoff = self._first.route(request)
        if handoff is None:
            handoff = downgrade_question_unsupported(self._fallback.route(request), request)
        self._last_handoff = handoff
        return handoff

    @staticmethod
    def _repeat_handoff(pending: RouteHandoff, trace: RouteTrace) -> RouteHandoff:
        """把"待确认任务被重复了一遍"重绑到**本轮**的 trace 上，并打上重复标记。

        路由仍是 `task`（⇒ 不会触发聊天/Provider），但 provider 换成
        `PENDING_CONFIRMATION_REPEAT_PROVIDER`：会话据此**保持**待确认、最多重问一次。
        """

        return RouteHandoff(
            trace=trace,
            route=pending.route,
            intent=pending.intent,
            handling_mode=pending.handling_mode,
            confidence=pending.confidence,
            slots=dict(pending.slots),
            required_confirmation=pending.required_confirmation,
            tool_scope=pending.tool_scope,
            router=RouterStatus(
                provider=PENDING_CONFIRMATION_REPEAT_PROVIDER,
                model_id=pending.router.model_id,
                model_version=pending.router.model_version,
                status="success",
                failure_code=None,
            ),
        )

    @staticmethod
    def _confirmed_handoff(pending: RouteHandoff, trace: RouteTrace) -> RouteHandoff:
        """把待确认任务重绑到**本轮**的 trace 上，并打上"确认"标记（一次性）。"""

        return RouteHandoff(
            trace=trace,
            route=pending.route,
            intent=pending.intent,
            handling_mode=pending.handling_mode,
            confidence=pending.confidence,
            slots=dict(pending.slots),
            required_confirmation=pending.required_confirmation,
            tool_scope=pending.tool_scope,
            router=RouterStatus(
                provider=PENDING_CONFIRMATION_PROVIDER,
                model_id=pending.router.model_id,
                model_version=pending.router.model_version,
                status="success",
                failure_code=None,
            ),
        )


__all__ = [
    "CANCEL_WORDS",
    "DEFAULT_CONFIRM_WINDOW_SECONDS",
    "DROPPED_CANCELLED",
    "DROPPED_REPLACED",
    "DROPPED_TIMEOUT",
    "PENDING_CONFIRMATION_PROVIDER",
    "PENDING_CONFIRMATION_REPEAT_PROVIDER",
    "PendingConfirmationRouter",
    "PendingTask",
    "REPEAT_OVERLAP_CHARACTERS",
    "is_pending_confirmation",
    "is_repeat_confirmation",
    "is_repeat_of_pending",
]
