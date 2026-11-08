"""P10 · T1 批次 3：语音任务轮的**状态机**（纯逻辑；播报与打印由调用方注入）。

它把"待确认路由"（`PendingConfirmationRouter`）与"派发会话"（`TaskDispatchSession` 的两步 API）
组合成语音侧需要的三件事，**不做任何 I/O**：

```text
新一轮开始前：expire()   —— 待确认已过期 ⇒ 作废 + 给"已取消"文案
本轮路由结果：
   ├─ 带确认标记            -> execute(prepared, answer=本轮 ASR 原文) -> 播报结构化摘要
   ├─ 重复句/回声标记       -> **保持**待确认：不取消、不重新出计划，最多重问一次
   ├─ 待确认被丢弃(超时/换目标) -> cancel(prepared) -> 播报"已取消"
   └─ 新的任务路由          -> prepare() -> 播报计划+提示 + router.arm()（**不执行**）
```

返回值是**有序的结果列表**（一次轮次里可能先"取消旧的"再"准备新的"，例如用户直接改口说
"打开记事本"），调用方按顺序播报/打印；返回空元组表示"本轮与任务无关"。

真机教训（2026-10-13，#8）：确认轮里 ASR 会把整句任务句（「小饼打开计算器。」）再识别一遍；旧口径
按"换了新任务"处理 ⇒ 作废计划 + 播「已取消」+ 重新出计划 ⇒ 死循环。现在**同目标/回声一律不动计划**，
只重问一次。重问的"有限次 + 每次各自计时"借自 Mycroft/OVOS `MycroftSkill.get_response(num_retries)`；
"撞上自己刚说/正在等的句子按回声丢弃"借自 AgenticSeek `sources/echo_filter.py`（判定在路由层
`pending_confirmation.is_repeat_of_pending`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from app.contracts.routing import RouteHandoff
from app.runtime.orchestration.local_task_router import ROUTE_UNSUPPORTED
from app.runtime.orchestration.pending_confirmation import (
    DROPPED_REPLACED,
    DROPPED_TIMEOUT,
    is_pending_confirmation,
    is_repeat_confirmation,
)
from app.runtime.orchestration.task_dispatch import (
    DENIED_TEXT,
    REASON_PREPARED,
    REASON_STALE_PREPARATION,
    STATUS_EXECUTED,
    STATUS_READY,
    STATUS_UNSUPPORTED,
    TIMEOUT_TEXT,
    unsupported_from_handoff,
)
from app.runtime.orchestration.task_flow import TASK_ROUTES
from app.runtime.orchestration.voice_lines import DEFAULT_VOICE_LINES, VoiceLines

KIND_AWAITING = "awaiting_confirmation"
KIND_EXECUTED = "executed"
KIND_DENIED = "denied"
KIND_UNSUPPORTED = "unsupported"
KIND_PREVIEW_FAILED = "preview_failed"
KIND_CANCELLED = "cancelled"
KIND_STALE = "stale_confirmation"

REASON_AWAITING = "task_awaiting_confirmation"
REASON_CANCELLED = "task_cancelled"
REASON_STALE = "task_stale_confirmation"
REASON_REPROMPT = "confirm_reprompt"
#: 重复句/回声（ASR 把待确认那句又识别一遍）：计划**保持有效**，本轮只重问一次。
REASON_REPEAT = "confirm_repeat"
#: 键盘取消（Esc）：与语音说「取消」同一条 cancel 路径。
REASON_KEY_CANCEL = "key_cancel"

#: 确认来源（如实记录，绝不把键盘事件伪装成 ASR 原文）。
CONFIRM_SOURCE_ASR = "asr"
CONFIRM_SOURCE_KEYBOARD = "keyboard"
#: 键盘确认时交给本地安全链的**固定答案**（`is_confirmation` 判定用；不是识别文本）。
CONFIRM_KEY_ANSWER = "确认"

CONFIRM_HINT = "请说「确认」执行；按 Esc 取消。"
#: 含糊回答（既不是确认、也不是新任务、也不是取消）时的唯一一次重问；#14 起从简，
#: 文案统一由 `VoiceLines` 出口提供（拟人化时只换这一处）。
#: 真机教训（2026-10-13）——用户说「确认执行」还没识别完，旧实现就把计划作废了。
REPROMPT_HINT = DEFAULT_VOICE_LINES.reprompt
HASH_PREFIX_LENGTH = 19  # "sha256:" + 前 12 位十六进制


def _short_hash(value: object) -> str | None:
    if type(value) is not str or not value:
        return None
    return value[:HASH_PREFIX_LENGTH]


def _text_or(value: object, fallback: object) -> str:
    """优先取**口语短文本**，没有再回落到完整文本（#14）。"""

    if type(value) is str and value.strip():
        return value
    return str(fallback)


@dataclass(frozen=True, slots=True)
class VoiceTaskOutcome:
    """一轮里与任务有关的一次结果（可播报/可打印/可记事实）。

    `spoken_text` = 语音播报用的**短**文本；`display_text` = 控制台/日志用的完整文本（可空，
    为空时按 `spoken_text` 处理）。真机反馈（2026-10-13）：全文念出来太长，但控制台仍要看得见
    工具名与风险等级。
    """

    kind: str
    status: str
    reason_code: str
    reason: str
    spoken_text: str | None
    plan_hash: str | None = None
    display_text: str | None = None
    confirm_source: str | None = None

    def to_details(self) -> dict[str, str]:
        details = {"task_kind": self.kind, "task_status": self.status, "task_reason": self.reason_code}
        if self.plan_hash is not None:
            details["task_plan_hash"] = self.plan_hash
        if self.confirm_source is not None:
            details["task_confirm_source"] = self.confirm_source
        return details

    @property
    def console_text(self) -> str | None:
        """控制台要打印的文本：完整文本优先，没有就用播报文本。"""

        if type(self.display_text) is str and self.display_text.strip():
            return self.display_text
        return self.spoken_text


class VoiceTaskTurn:
    """语音侧的任务轮状态机（router / dispatcher 都按鸭子类型使用，便于离线替换）。"""

    def __init__(
        self,
        *,
        router: Any,
        dispatcher: Any,
        clock: Callable[[], float] = time.monotonic,
        hint: str = CONFIRM_HINT,
        voice_lines: VoiceLines | None = None,
    ) -> None:
        if not callable(getattr(router, "arm", None)):
            raise TypeError("router 必须提供 arm()")
        for name in ("prepare", "execute", "cancel"):
            if not callable(getattr(dispatcher, name, None)):
                raise TypeError(f"dispatcher 必须提供 {name}()")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        if type(hint) is not str or not hint.strip():
            raise ValueError("hint 必须是非空字符串")
        if voice_lines is not None and not isinstance(voice_lines, VoiceLines):
            raise TypeError("voice_lines 必须是 VoiceLines 或 None")
        self._router = router
        self._dispatcher = dispatcher
        self._clock = clock
        self._hint = hint
        self._lines = voice_lines or DEFAULT_VOICE_LINES
        self._prepared: Any | None = None
        self._last_kind: str | None = None
        #: 含糊回答只重问一次（避免反复念叨）；新计划/新确认轮会重置
        self._reprompted = False

    @property
    def pending(self) -> Any | None:
        return self._prepared

    @property
    def last_kind(self) -> str | None:
        return self._last_kind

    def expire(self) -> VoiceTaskOutcome | None:
        """待确认窗口已过 ⇒ 作废并给出"已取消"文案（由调用方在下一轮开始前调用）。"""

        pending = getattr(self._router, "pending", None)
        prepared = self._prepared
        if prepared is None or pending is None:
            return None
        deadline = getattr(pending, "deadline", None)
        if type(deadline) not in (int, float) or self._clock() <= deadline:
            return None
        clear = getattr(self._router, "clear", None)
        if callable(clear):
            clear()
        self._prepared = None
        self._reprompted = False
        result = self._dispatcher.cancel(prepared)
        # 2026-10-23：超时用**专用**文案（"等太久了"），不再与"换目标/按键取消"共用
        # "已取消，未执行任何操作。"——真机上用户无法区分"是我取消的"还是"窗口过期了"。
        text = TIMEOUT_TEXT
        return self._record(
            VoiceTaskOutcome(
                kind=KIND_CANCELLED,
                status=str(getattr(result, "status", KIND_DENIED)),
                reason_code=DROPPED_TIMEOUT,
                reason=REASON_CANCELLED,
                spoken_text=text,
                plan_hash=_short_hash(getattr(result, "plan_hash", None)),
                display_text=text,
            )
        )

    def handle(self, handoff: object) -> tuple[VoiceTaskOutcome, ...]:
        """按本轮路由结果推进状态机；返回有序结果（可能为空）。"""

        outcomes: list[VoiceTaskOutcome] = []
        if is_repeat_confirmation(handoff):
            # 重复句/回声：计划保持待确认，不取消、不重新出计划（真机缺陷 #8）。
            outcome = self._keep_pending()
            if outcome is not None:
                outcomes.append(outcome)
            return tuple(outcomes)
        if is_pending_confirmation(handoff):
            outcomes.append(self._confirm(handoff))
            return tuple(outcomes)
        dropped = self._take_dropped()
        if dropped is not None and self._prepared is not None:
            prepared = self._prepared
            self._prepared = None
            self._reprompted = False
            result = self._dispatcher.cancel(prepared)
            text = str(getattr(result, "text", DENIED_TEXT))
            # D-A（2026-10-14 真机实测）："换目标"时旧计划**仍要作废**，但**不再连播"已取消"** ——
            # 真机一次「已取消 + 新计划」要念 3.5 s + 6.1 s ≈ 9.6 s，用户反馈"说完话好久没反应"。
            # 于是这里只把"已取消"留在控制台/审计（`display_text`），语音只念新计划；
            # 超时、按键取消等**其它**丢弃路径仍照旧播报"已取消"（用户需要知道计划没了）。
            silent_cancel = dropped == DROPPED_REPLACED and self._announces_itself(handoff)
            outcomes.append(
                self._record(
                    VoiceTaskOutcome(
                        kind=KIND_CANCELLED,
                        status=str(getattr(result, "status", KIND_DENIED)),
                        reason_code=dropped,
                        reason=REASON_CANCELLED,
                        spoken_text=(
                            None
                            if silent_cancel
                            else _text_or(getattr(result, "spoken_text", None), text)
                        ),
                        plan_hash=_short_hash(getattr(result, "plan_hash", None)),
                        display_text=text,
                    )
                )
            )
        elif self._take_vague() and self._prepared is not None and not self._reprompted:
            # 含糊回答**不作废计划**：只重问一次，窗口内继续等「确认」
            self._reprompted = True
            outcomes.append(
                self._record(
                    VoiceTaskOutcome(
                        kind=KIND_AWAITING,
                        status=STATUS_READY,
                        reason_code=REASON_REPROMPT,
                        reason=REASON_AWAITING,
                        spoken_text=self._lines.reprompt_line(),
                        plan_hash=_short_hash(getattr(self._prepared, "plan_hash", None)),
                    )
                )
            )
        if isinstance(handoff, RouteHandoff) and handoff.route == ROUTE_UNSUPPORTED:
            # #12：本批明确不支持的任务句 —— 如实播报/打印，**不 arm、不 prepare、绝不执行**。
            outcomes.append(self._unsupported(handoff))
        elif isinstance(handoff, RouteHandoff) and handoff.route in TASK_ROUTES:
            outcomes.append(self._prepare(handoff))
        return tuple(outcomes)

    # ---- 内部 ---------------------------------------------------------------

    @staticmethod
    def _announces_itself(handoff: object) -> bool:
        """本轮交接是否**自己会播报**（新任务计划 / 本批不支持说明）。

        真机动机（2026-10-14）：换目标时"先念已取消、再念新内容"一共要 9.6 s；用户只关心新内容。
        所以只要本轮**会自己播报**，就略过"已取消"的语音（`display_text` 仍留给控制台/审计）；
        超时、含糊等不会自己播报的丢弃路径仍照旧把"已取消"念出来。
        """

        return isinstance(handoff, RouteHandoff) and (
            handoff.route in TASK_ROUTES or handoff.route == ROUTE_UNSUPPORTED
        )

    def _keep_pending(self) -> VoiceTaskOutcome | None:
        """重复句/回声：**保持**待确认，只重问一次（借 Mycroft 的 `num_retries` 口径）。

        - 首次重复 ⇒ 复读一次提示，并让路由把窗口**重新计时**（重问后就该重新算等待时间）；
        - 第二次及以后 ⇒ 什么都不说（不再念叨），计划仍然有效；
        - 没有已冻结的计划 ⇒ 什么都不做（不猜、不 arm）。
        """

        prepared = self._prepared
        if prepared is None or self._reprompted:
            return None
        self._reprompted = True
        self._rearm_window()
        return self._record(
            VoiceTaskOutcome(
                kind=KIND_AWAITING,
                status=STATUS_READY,
                reason_code=REASON_REPEAT,
                reason=REASON_AWAITING,
                spoken_text=self._lines.reprompt_line(),
                plan_hash=_short_hash(getattr(prepared, "plan_hash", None)),
            )
        )

    def _rearm_window(self) -> None:
        """让路由把待确认窗口从现在重新计时（路由不支持时静默跳过，任务事实不受影响）。"""

        rearm = getattr(self._router, "rearm", None)
        if not callable(rearm):
            return
        try:
            rearm()
        except Exception:  # noqa: BLE001 - 重新计时失败不得改变任务事实
            return

    def _confirm(self, handoff: RouteHandoff) -> VoiceTaskOutcome:
        prepared = self._prepared
        self._prepared = None
        self._reprompted = False
        if prepared is None:
            return self._record(
                VoiceTaskOutcome(
                    kind=KIND_STALE,
                    status=KIND_DENIED,
                    reason_code=REASON_STALE_PREPARATION,
                    reason=REASON_STALE,
                    spoken_text=DENIED_TEXT,
                )
            )
        answer = getattr(getattr(handoff, "trace", None), "raw_text", None)
        result = self._dispatcher.execute(prepared, answer=answer)
        status = str(getattr(result, "status", KIND_DENIED))
        text = str(getattr(result, "text", DENIED_TEXT))
        kind = KIND_EXECUTED if status == STATUS_EXECUTED else KIND_DENIED
        return self._record(
            VoiceTaskOutcome(
                kind=kind,
                status=status,
                reason_code=str(getattr(result, "reason_code", "unknown")),
                reason=f"task_{status}",
                spoken_text=_text_or(getattr(result, "spoken_text", None), text),
                plan_hash=_short_hash(getattr(result, "plan_hash", None)),
                display_text=text,
                confirm_source=CONFIRM_SOURCE_ASR,
            )
        )

    def confirm_by_keyboard(self) -> VoiceTaskOutcome | None:
        """键盘「确认」（Enter/空格）：与语音确认走**同一条** `execute()` 链路。

        - 答案来自**本地按键事件**（`CONFIRM_KEY_ANSWER`），绝不伪装成 ASR 原文；
        - 一次性消费：没有已冻结计划时返回 ``None``（不猜、不 arm、不播报）；
        - 同时清掉路由的待确认状态，避免事后又说「确认」而重复执行。
        """

        prepared = self._prepared
        if prepared is None:
            return None
        self._prepared = None
        self._reprompted = False
        self._clear_router()
        result = self._dispatcher.execute(prepared, answer=CONFIRM_KEY_ANSWER)
        status = str(getattr(result, "status", KIND_DENIED))
        text = str(getattr(result, "text", DENIED_TEXT))
        kind = KIND_EXECUTED if status == STATUS_EXECUTED else KIND_DENIED
        return self._record(
            VoiceTaskOutcome(
                kind=kind,
                status=status,
                reason_code=str(getattr(result, "reason_code", "unknown")),
                reason=f"task_{status}",
                spoken_text=_text_or(getattr(result, "spoken_text", None), text),
                plan_hash=_short_hash(getattr(result, "plan_hash", None)),
                display_text=text,
                confirm_source=CONFIRM_SOURCE_KEYBOARD,
            )
        )

    def cancel_by_keyboard(self) -> VoiceTaskOutcome | None:
        """键盘「取消」（Esc）：复用 cancel 路径并播「已取消」；没有待确认计划时返回 ``None``。"""

        prepared = self._prepared
        if prepared is None:
            return None
        self._prepared = None
        self._reprompted = False
        self._clear_router()
        result = self._dispatcher.cancel(prepared)
        text = str(getattr(result, "text", DENIED_TEXT))
        return self._record(
            VoiceTaskOutcome(
                kind=KIND_CANCELLED,
                status=str(getattr(result, "status", KIND_DENIED)),
                reason_code=REASON_KEY_CANCEL,
                reason=REASON_CANCELLED,
                spoken_text=_text_or(getattr(result, "spoken_text", None), text),
                plan_hash=_short_hash(getattr(result, "plan_hash", None)),
                display_text=text,
                confirm_source=CONFIRM_SOURCE_KEYBOARD,
            )
        )

    def _clear_router(self) -> None:
        clear = getattr(self._router, "clear", None)
        if not callable(clear):
            return
        try:
            clear()
        except Exception:  # noqa: BLE001 - 清状态失败不得影响任务事实
            return

    def _unsupported(self, handoff: RouteHandoff) -> VoiceTaskOutcome:
        """#12：`route="unsupported"` ⇒ 语音念`VoiceLines.unsupported`、控制台/审计看全文。

        原因短码如实取 `handoff.intent`（如 `unimplemented_capability`），具体操作（`close_application`
        /`delete_files`）只进全文；**不准备计划、不 arm 路由、不产生任何执行**。
        """

        display = unsupported_from_handoff(handoff) or self._lines.unsupported_line()
        return self._record(
            VoiceTaskOutcome(
                kind=KIND_UNSUPPORTED,
                status=STATUS_UNSUPPORTED,
                reason_code=str(getattr(handoff, "intent", "") or "unimplemented_capability"),
                reason=f"task_{KIND_UNSUPPORTED}",
                spoken_text=self._lines.unsupported_line(),
                display_text=display,
            )
        )

    def _prepare(self, handoff: RouteHandoff) -> VoiceTaskOutcome:
        preparation = self._dispatcher.prepare(handoff)
        prepared = getattr(preparation, "prepared", None)
        status = str(getattr(preparation, "status", "preview_failed"))
        reason_code = str(getattr(preparation, "reason_code", "preview_failed"))
        text = str(getattr(preparation, "text", ""))
        spoken = getattr(preparation, "spoken_text", None)
        if prepared is None:
            kind = KIND_UNSUPPORTED if status == STATUS_UNSUPPORTED else KIND_PREVIEW_FAILED
            short = _text_or(spoken, text)
            return self._record(
                VoiceTaskOutcome(
                    kind=kind,
                    status=status,
                    reason_code=reason_code,
                    reason=f"task_{kind}",
                    spoken_text=short or None,
                    display_text=text or None,
                )
            )
        self._prepared = prepared
        self._reprompted = False
        self._router.arm(handoff)
        fallback = f"{text}\n{self._hint}" if text else self._hint
        announce = _text_or(spoken, fallback)
        return self._record(
            VoiceTaskOutcome(
                kind=KIND_AWAITING,
                status=STATUS_READY,
                reason_code=REASON_PREPARED,
                reason=REASON_AWAITING,
                spoken_text=announce,
                plan_hash=_short_hash(getattr(prepared, "plan_hash", None)),
                # D-B（2026-10-14）：语音只念短句，但"怎么说确认"这句提示要留在控制台里。
                display_text=fallback,
            )
        )

    def _take_dropped(self) -> str | None:
        take = getattr(self._router, "take_dropped", None)
        if not callable(take):
            return None
        value = take()
        return value if type(value) is str and value else None

    def _take_vague(self) -> bool:
        take = getattr(self._router, "take_vague", None)
        if not callable(take):
            return False
        return bool(take())

    def _record(self, outcome: VoiceTaskOutcome) -> VoiceTaskOutcome:
        self._last_kind = outcome.kind
        return outcome


__all__ = [
    "CONFIRM_HINT",
    "CONFIRM_KEY_ANSWER",
    "CONFIRM_SOURCE_ASR",
    "CONFIRM_SOURCE_KEYBOARD",
    "KIND_AWAITING",
    "KIND_CANCELLED",
    "KIND_DENIED",
    "KIND_EXECUTED",
    "KIND_PREVIEW_FAILED",
    "KIND_STALE",
    "KIND_UNSUPPORTED",
    "REASON_AWAITING",
    "REASON_CANCELLED",
    "REASON_KEY_CANCEL",
    "REASON_REPEAT",
    "REASON_REPROMPT",
    "REASON_STALE",
    "REPROMPT_HINT",
    "VoiceTaskOutcome",
    "VoiceTaskTurn",
]
