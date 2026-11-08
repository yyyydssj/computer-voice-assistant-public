"""P10 · T1 批次 1：**本地任务路由**（确定性句式匹配，离线、零外部依赖）。

设计依据（`implementation_plan/research/2026-10-13-task-exec-open-app-research.md` §2/§3）：

- 匹配前先归一化：NFKC + 去首尾标点 + 折叠空白 + 跳过礼貌前缀/尾语气词 —— 对应 HA intents 的
  ``ignore_whitespace``（中文/日文）与 ``skip_words``（"please"）两条经验；
- 只写**固定句式**，槽"要么必有、要么整段可选"，**不在可选/交替里引槽** —— HassIL 的硬约束；
- 命中但没给应用名 ⇒ ``ambiguous/missing_target``（要求澄清，**绝不猜、绝不执行**）；
- 命中**本批明确不支持**的任务句式（关闭/退出应用、删除文件等，见 `match_unsupported_command`）
  ⇒ ``unsupported/unimplemented_capability``（如实拒绝，**不再静默落到普通聊天**，P10 · T1 #12）；
  但**纯询问**（只有疑问标记、没有祈使标记的「能不能退出程序」）不算命令，交聊天
  （2026-11-08 口径 A；带「帮我/给我/请你」的「能不能帮我关闭计算器」仍按 #12 拒绝）；
- 没命中 ⇒ 返回 ``None``，由上层 fallback（真实 Provider / 本地聊天路由）接手。

本模块**不**校验白名单、不产生计划、不执行任何东西：它只产出"不可信的路线建议"。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol, Sequence

from app.contracts.routing import RouteHandoff, RouteTrace, RouterStatus, SlotCandidate
from app.routing import RouteInput
from app.runtime.orchestration.app_launch import AppLaunchError, resolve_application_id

ROUTER_PROVIDER = "local"
ROUTER_MODEL_ID = "task-rule-router"
ROUTER_MODEL_VERSION = "1"

#: 只用于**首尾**剥离，避免把句中标点（如"计算器，打开"）误当分隔
STRIP_CHARACTERS = " \t\r\n\u3000，。！？、；：,.!?;:\"'“”‘’（）()【】[]{}<>《》「」『』~～-—_·…"

#: 礼貌前缀（按长度降序匹配，避免"请"吃掉"请帮我"）
_HONORIFICS = (
    "请帮我",
    "麻烦帮我",
    "能不能帮我",
    "可以帮我",
    "帮我",
    "帮忙",
    "麻烦",
    "请",
    "能不能",
    "可不可以",
    "可以",
    "给我",
    "替我",
    "我要",
    "我想",
)

#: 句尾语气词
_TAILS = ("好吗", "可以吗", "行吗", "吧", "啊", "呀", "呢", "哦", "嘛")

#: 真机教训（2026-10-13，`--voice --auto` 实测）：ASR 会把**唤醒名**一起识别进来，
#: 例如用户说"打开计算器"却识别成「小饼打开计算器。」——唤醒名属"称呼"，与礼貌前缀同性质，
#: 因此在归一化阶段按**调用方显式给出的名字**（`--keyword`，默认「小饼」）剥掉。
#: 只剥**精确等于**给定名字的开头，不做任何猜测。

#: 中文前缀句式（在**紧凑形式**上匹配：空白已被去掉）
_PREFIX_VERBS = ("打开", "启动", "运行", "开启")
#: 中文后缀句式："把记事本打开"
_SUFFIX_VERBS = ("打开", "启动", "运行", "开启")
#: 英文前缀句式（在保留单空格的归一化形式上匹配）
_EN_VERBS = ("open", "launch", "start", "run")

#: P10 · T1 #12：**本批明确不支持**的任务句式（可识别 ⇒ 必须如实拒绝，不能静默变聊天）。
#: 路线是契约原生的 ``unsupported/unimplemented_capability``（`app/contracts/routing.py`）。
ROUTE_UNSUPPORTED = "unsupported"
INTENT_UNSUPPORTED = "unimplemented_capability"
UNSUPPORTED_HANDLING_MODE = "reject"
UNSUPPORTED_CONFIDENCE = 0.9

#: Provider 把"疑问/否定/假设句"判成不支持时的**确定性兜底**（2026-10-23 真机缺陷）。
#: 借 Mycroft/OVOS 的 `continue_chat` 弃权默认（小智服务端意图提示词同理）：没命中任务就
#: 回落聊天；只有**明确命令句**才算"本批不支持"。
QUESTION_FALLBACK_PROVIDER = "question_fallback"
ROUTE_CHAT = "chat"
INTENT_CHAT = "general_chat"
CHAT_HANDLING_MODE = "chat_reply"

#: 操作短码（进 `slots["operation"]`，供审计/控制台区分"是哪类不支持"）
OP_CLOSE_APPLICATION = "close_application"
OP_DELETE_FILES = "delete_files"

#: 关闭/退出类动词（**按长度降序**，否则单字「关」会吃掉「关闭/关掉」）
#: 2026-11-08 真机补：「关计算器」这种省掉"闭"的说法 ASR 很常见，单字「关」放最后兜底。
UNSUPPORTED_CLOSE_VERBS = ("关闭", "关掉", "关上", "退出", "结束", "关")
#: 删除/移除类动词
UNSUPPORTED_DELETE_VERBS = ("删除", "删掉", "移除", "清空")

#: 关闭/退出类可接受的"对象"泛称（**必须**有对象才判命中，避免"关闭"两字把闲聊吃掉）
UNSUPPORTED_APP_OBJECTS = ("应用", "程序", "窗口", "软件", "进程", "它")
#: 删除类可接受的"对象"泛称
UNSUPPORTED_PATH_OBJECTS = ("文件", "文件夹", "目录", "路径", "桌面", "回收站", "剪贴板")

#: 疑问/否定/假设标记：出现即**不**判为"不支持的任务句"（继续交给 fallback 聊天）。
#: 借 HA intents/HassIL 的"只认固定句式，不做模糊猜测"口径：宁可漏判也不误判。
UNSUPPORTED_BLOCKERS = (
    "吗", "？", "?", "什么", "怎么", "为什么", "如何", "不要", "别", "不能", "不用",
    "不需要", "会不会", "能不能", "可以吗", "假设", "如果",
)

#: 疑问标记（2026-11-08 口径 A）：句首礼貌前缀**不改变句子类型**。判定"是询问还是命令"
#: 要看**原句**：只有疑问标记、没有祈使标记 ⇒ 在问"能不能"，交聊天（由模型如实回答做不到）；
#: 带祈使标记 ⇒ 真的是请求，仍按"本批不支持"如实拒绝（#12 口径不变）。
#: 背景：`normalize_command()` 会把句首「能不能」当礼貌前缀剥掉，于是「能不能退出程序」被当成
#: 「退出程序」拒绝，而「你能不能退出程序」却走聊天——同一句话加不加"你"结果相反。
QUESTION_MARKERS = ("吗", "？", "?", "能不能", "可不可以", "可以吗", "好吗", "行吗", "会不会")
#: 祈使标记：出现即视为"要求现在就做"，覆盖疑问标记（中文里"能不能帮我 X"是常见礼貌命令）。
IMPERATIVE_MARKERS = ("帮我", "帮忙", "给我", "替我", "请你", "麻烦你")
#: **只属于疑问性质**的阻断词（2026-11-08 口径 A 的完整实现）：带祈使标记时它们**不再**阻断
#: ——「你能不能帮我关闭计算器？」是请求，不是询问（#12 口径）。否定/假设类阻断词（不要/别/如果…）
#: 仍然阻断，与祈使标记无关。
QUESTION_ONLY_BLOCKERS = ("吗", "？", "?", "能不能", "可以吗", "会不会")
#: 否定/假设类阻断词：**永远**保持"交聊天"（"不要帮我关闭计算器"是劝阻，不是请求）。
_NEGATION_BLOCKERS = ("不要", "别", "不能", "不用", "不需要", "假设", "如果")
#: 句首口语填充/连接词（2026-11-08 真机教训）：ASR 常把「那/嗯/就是」一起识别进来，
#: 只在**本检测**里剥（不改 `normalize_command()` 的公共行为），否则「那你能不能帮我关闭计算器」
#: 会因为句首多一个"那"而匹配不上。
_FILLER_PREFIXES = ("那个", "那么", "那", "嗯", "呃", "哦", "就是", "所以", "然后", "可是", "但是")

#: 看起来像路径/文件名（删除类用）：带分隔符，或以 `.扩展名` 结尾
_PATH_LIKE = re.compile(r"[\\/]|\.[A-Za-z0-9]{1,6}$")

#: #12 专用：`normalize_command` 之后仍可能剩下的"你/您 + 礼貌"前缀（ASR 常说「你能不能帮我关闭计算器」）。
#: 只在**本检测**里额外剥离，不改 `normalize_command()` 的公共行为（既有路由测试口径不变）。
_EXTRA_PREFIXES = (
    "你能不能帮我",
    "你可不可以帮我",
    "你能帮我",
    "你可以帮我",
    "你能帮忙",
    "你帮我",
    "您帮我",
    # 2026-11-08：祈使标记本身也可能以「能」开头（「能帮我关闭计算器吗」），
    # 与上面一组同性质，剥掉后按请求处理（#12 口径）。
    "能不能帮我",
    "能帮我",
    "能帮忙",
    "能给我",
)

_WHITESPACE = re.compile(r"\s+")


class RouterLike(Protocol):
    """`TaskFirstRouter` 需要的最小面：返回交接或 ``None``（未命中）。"""

    def route(self, request: RouteInput) -> RouteHandoff | None: ...


def _clean_names(names: Sequence[str]) -> tuple[str, ...]:
    """校验并归一化"称呼/唤醒名"清单：去空、去重、按长度降序（避免短名吃掉长名）。"""

    cleaned: list[str] = []
    for item in names:
        if type(item) is not str:
            raise TypeError("names 只能包含字符串")
        value = unicodedata.normalize("NFKC", item).strip(STRIP_CHARACTERS + " ")
        if value and value not in cleaned:
            cleaned.append(value)
    return tuple(sorted(cleaned, key=len, reverse=True))


def normalize_command(text: str, *, names: Sequence[str] = ()) -> str:
    """归一化一条口令：NFKC → 折叠空白 → 去首尾标点 → 去**称呼/唤醒名**与礼貌前缀/尾语气词。

    只做**确定性的文本清理**，不改变语序、不猜测意图；空输入返回空串（调用方按未命中处理）。
    """

    if type(text) is not str:
        raise TypeError("text 必须是字符串")
    value = unicodedata.normalize("NFKC", text).strip()
    value = _WHITESPACE.sub(" ", value).strip(STRIP_CHARACTERS)
    prefixes = _clean_names(names) + _HONORIFICS
    for _ in range(4):
        folded = value.casefold()
        for token in prefixes:
            if folded.startswith(token.casefold()):
                value = value[len(token) :].strip(STRIP_CHARACTERS + " ")
                break
        else:
            break
    for _ in range(3):
        for token in _TAILS:
            if value.endswith(token):
                value = value[: -len(token)].strip(STRIP_CHARACTERS + " ")
                break
        else:
            break
    return value.strip(STRIP_CHARACTERS + " ")


def _compact(value: str) -> str:
    """去掉所有空白（中文说话常带空格，或 ASR 会插空格）。"""

    return _WHITESPACE.sub("", value)


def _is_polite_question(text: str) -> bool:
    """原句是"在问能不能"（询问），而不是"要求现在就做"（命令）。

    2026-11-08 口径 A：句首礼貌前缀不改变句子类型——
    - 有疑问标记、**且没有**祈使标记 ⇒ 询问（如「能不能退出程序」「你能不能退出程序」）；
    - 有祈使标记（帮我/给我/请你…）⇒ 请求（如「能不能帮我关闭计算器」），#12 口径不变。

    在**原始文本**上判断（`normalize_command` 已经把「能不能」这类前缀剥掉了，看不出来）。
    """

    compact = _compact(unicodedata.normalize("NFKC", text))
    if not any(marker in compact for marker in QUESTION_MARKERS):
        return False
    return not any(marker in compact for marker in IMPERATIVE_MARKERS)


def match_application_name(text: str, *, names: Sequence[str] = ()) -> tuple[bool, str]:
    """返回 ``(是否命中打开句式, 应用名)``；未命中时应用名为空串。

    只认**明确句式**：命中句式但名字为空 ⇒ ``(True, "")``，由调用方转成"要求澄清"，
    **绝不**把整句当成应用名。``names`` 是要在句首剥掉的称呼/唤醒名（见 `normalize_command`）。
    """

    normalized = normalize_command(text, names=names)
    if not normalized:
        return False, ""
    compact = _compact(normalized)
    for verb in _PREFIX_VERBS:
        if compact.startswith(verb):
            return True, compact[len(verb) :].strip(STRIP_CHARACTERS)
    if compact.startswith("把"):
        for verb in _SUFFIX_VERBS:
            if compact.endswith(verb) and len(compact) > len(verb) + 1:
                return True, compact[1 : -len(verb)].strip(STRIP_CHARACTERS)
    lowered = normalized.casefold()
    for verb in _EN_VERBS:
        if lowered == verb:
            return True, ""
        if lowered.startswith(verb + " "):
            return True, normalized[len(verb) :].strip(STRIP_CHARACTERS + " ")
    return False, ""


def _is_known_application(value: str) -> bool:
    """目标是不是**白名单里的应用**（别名解析成功即算；解析不了一律 False）。"""

    try:
        resolve_application_id(value)
    except AppLaunchError:
        return False
    except Exception:  # noqa: BLE001 - 解析异常按"不认得"处理（宁可漏判，也不误判）
        return False
    return True


def _has_unsupported_object(target: str, *, application: bool) -> bool:
    """判断动词后面的"对象"是否足够明确（**没有对象就不算命中**）。"""

    value = target.strip(STRIP_CHARACTERS + " ")
    if not value:
        return False
    if application:
        if any(item in value for item in UNSUPPORTED_APP_OBJECTS):
            return True
        return _is_known_application(value)
    if any(item in value for item in UNSUPPORTED_PATH_OBJECTS):
        return True
    return bool(_PATH_LIKE.search(value))


def match_unsupported_command(text: str, *, names: Sequence[str] = ()) -> str | None:
    """返回**操作短码**（`close_application`/`delete_files`）；不是本批不支持的任务句则返回 ``None``。

    只认两类**固定句式**（P10 · T1 #12 的口径）：

    ```text
    前缀式：「关闭计算器」「退出程序」「删除文件」「删掉 report.txt」
    后缀式：「把记事本关掉」「把桌面上的文件删除」
    ```

    判定必须同时满足：①归一化后以动词开头（或"把…动词"结尾）；②**有明确对象**
    （白名单应用/别名，或"应用/程序/窗口/文件/文件夹/目录/桌面"等泛称，或像路径/文件名的串）；
    ③整句不含疑问/否定标记（"关闭是什么意思""不要关闭计算器"⇒ 不命中，交回聊天），
    **但带祈使标记时**「吗/？/能不能/可以吗/会不会」这类纯疑问阻断词不再阻断；
    ④**不是"纯询问"**（2026-11-08 口径 A）：只有疑问标记、没有祈使标记的「能不能退出程序」
    ⇒ 不命中，交聊天；带「帮我/给我/请你」等祈使标记的「能不能帮我关闭计算器？」⇒ 仍命中
    （#12 口径不变）。句首口语填充词（那/嗯/就是…）只在本检测里剥掉。

    **没有对象**（如只说「删除」「关闭」）一律不命中：宁可漏判，也不把闲聊变成拒绝。
    """

    if type(text) is not str:
        raise TypeError("text 必须是字符串")
    # 2026-11-08 口径 A：句首礼貌前缀不改变句子类型——纯询问（无祈使标记）交聊天。
    if _is_polite_question(text):
        return None
    # 祈使标记（帮我/给我/请你…）出现时，句末的「吗/？」等**纯疑问阻断词**不再阻断：
    # 「你能不能帮我关闭计算器？」在中文里是请求（#12 口径），不是询问。
    raw_compact = _compact(unicodedata.normalize("NFKC", text))
    imperative = any(marker in raw_compact for marker in IMPERATIVE_MARKERS)
    normalized = normalize_command(text, names=names)
    if not normalized:
        return None
    compact = _compact(normalized)
    prefixes = tuple(sorted(_EXTRA_PREFIXES + _FILLER_PREFIXES, key=len, reverse=True))
    for _ in range(3):
        for prefix in prefixes:
            if compact.startswith(prefix) and len(compact) > len(prefix):
                compact = compact[len(prefix) :].strip(STRIP_CHARACTERS)
                break
        else:
            break
    if not compact:
        return None
    # 带祈使标记时，句末「吗」也是纯疑问标记（"帮我关闭计算器吗"），不能粘在对象上导致漏判。
    if imperative:
        compact = compact.rstrip("吗？?")
        if not compact:
            return None
    blockers = (
        tuple(marker for marker in UNSUPPORTED_BLOCKERS if marker not in QUESTION_ONLY_BLOCKERS)
        if imperative
        else UNSUPPORTED_BLOCKERS
    )
    if any(marker in compact for marker in blockers):
        return None
    for verb in UNSUPPORTED_CLOSE_VERBS:
        if compact.startswith(verb) and _has_unsupported_object(
            compact[len(verb) :], application=True
        ):
            return OP_CLOSE_APPLICATION
    for verb in UNSUPPORTED_DELETE_VERBS:
        if compact.startswith(verb) and _has_unsupported_object(
            compact[len(verb) :], application=False
        ):
            return OP_DELETE_FILES
    if compact.startswith("把") and len(compact) > 2:
        for verb in UNSUPPORTED_CLOSE_VERBS:
            if compact.endswith(verb) and _has_unsupported_object(
                compact[1 : -len(verb)], application=True
            ):
                return OP_CLOSE_APPLICATION
        for verb in UNSUPPORTED_DELETE_VERBS:
            if compact.endswith(verb) and _has_unsupported_object(
                compact[1 : -len(verb)], application=False
            ):
                return OP_DELETE_FILES
    return None


def downgrade_question_unsupported(handoff: object, request: object) -> object:
    """把"疑问/否定/假设句被判成不支持"的交接**确定性回落**成普通聊天。

    真机缺陷（2026-10-23，`--voice --auto`）：说「你能关闭应用吗？」时 Provider 判成
    ``unsupported/unimplemented_capability``，于是如实播报"这条任务本批暂不支持"——用户只是
    问一句，不是下命令。本地句式层本来就有 :data:`UNSUPPORTED_BLOCKERS` 白名单（疑问/否定/
    假设直接放行），但 Provider 路由不受它约束；这里补一条**与模型无关**的兜底，口径是
    "宁可多聊一句，也不误拒"。只有**明确命令句**才保持 ``unsupported``。

    非 ``unsupported`` 交接、或没有请求文本时原样返回（零行为变化）。

    2026-11-08 口径 A 的补充：**带祈使标记且不是否定/假设**的句子（如「你能不能帮我关闭计算器？」）
    是请求，保持 ``unsupported``，不在这里被"疑问兜底"翻成聊天（#12 口径）。
    """

    if getattr(handoff, "route", None) != ROUTE_UNSUPPORTED:
        return handoff
    raw_text = getattr(getattr(request, "trace", None), "raw_text", "")
    compact = _compact(raw_text if isinstance(raw_text, str) else "")
    if not any(marker in compact for marker in UNSUPPORTED_BLOCKERS):
        return handoff
    # 注意「能不能」里天然含「不能」：判断否定前先去掉疑问短语，否则礼貌请求会被误判成劝阻。
    without_question = compact.replace("能不能", "").replace("可不可以", "")
    if any(marker in compact for marker in IMPERATIVE_MARKERS) and not any(
        marker in without_question for marker in _NEGATION_BLOCKERS
    ):
        # 祈使请求（"你能不能帮我关闭计算器？"）⇒ 保持如实拒绝（#12 口径）。
        return handoff
    router = getattr(handoff, "router", None)
    return RouteHandoff(
        trace=getattr(handoff, "trace"),
        route=ROUTE_CHAT,
        intent=INTENT_CHAT,
        handling_mode=CHAT_HANDLING_MODE,
        confidence=max(float(getattr(handoff, "confidence", 0.0) or 0.0), 0.80),
        slots={},
        required_confirmation="none",
        tool_scope=(),
        router=RouterStatus(
            provider=QUESTION_FALLBACK_PROVIDER,
            model_id=getattr(router, "model_id", ROUTER_MODEL_ID),
            model_version=getattr(router, "model_version", ROUTER_MODEL_VERSION),
            status="success",
            failure_code=None,
        ),
    )


def _status() -> RouterStatus:
    return RouterStatus(
        provider=ROUTER_PROVIDER,
        model_id=ROUTER_MODEL_ID,
        model_version=ROUTER_MODEL_VERSION,
        status="success",
        failure_code=None,
    )


class LocalTaskRouter:
    """把明确句式映射成 ``RouteHandoff(task/open_application)``；其余一律返回 ``None``。

    ``names`` 是要在句首剥掉的称呼/唤醒名（默认空 ⇒ 与批次 1 行为逐字节一致）；装配层会把
    `--keyword`（默认「小饼」）传进来，因为 ASR 常把唤醒名一起识别出来。

    ``detect_unsupported``（默认 `True`，P10 · T1 #12）：把"本批明确不支持"的任务句式
    （关闭/退出应用、删除文件）转成 ``unsupported/unimplemented_capability`` 交接，
    交给上层**如实播报/打印**，而不是静默落进普通聊天。置 `False` 可退回批次 1 行为。
    """

    def __init__(self, *, names: Sequence[str] = (), detect_unsupported: bool = True) -> None:
        if type(detect_unsupported) is not bool:
            raise TypeError("detect_unsupported 必须是布尔值")
        self._names = _clean_names(names)
        self._detect_unsupported = detect_unsupported

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    @property
    def detect_unsupported(self) -> bool:
        return self._detect_unsupported

    def route(self, request: RouteInput) -> RouteHandoff | None:
        if not isinstance(request, RouteInput):
            raise TypeError("request 必须是 RouteInput")
        trace: RouteTrace = request.trace
        matched, name = match_application_name(trace.raw_text, names=self._names)
        if not matched:
            operation = (
                match_unsupported_command(trace.raw_text, names=self._names)
                if self._detect_unsupported
                else None
            )
            if operation is None:
                return None
            # #12：本批不支持的能力 —— 如实拒绝，绝不静默变聊天、绝不执行。
            return RouteHandoff(
                trace=trace,
                route=ROUTE_UNSUPPORTED,
                intent=INTENT_UNSUPPORTED,
                handling_mode=UNSUPPORTED_HANDLING_MODE,
                confidence=UNSUPPORTED_CONFIDENCE,
                slots={
                    "operation": SlotCandidate(
                        value=operation,
                        source="normalization",
                        confidence=UNSUPPORTED_CONFIDENCE,
                        confirmed=False,
                    )
                },
                required_confirmation="none",
                tool_scope=(),
                router=_status(),
            )
        if not name:
            # 命中句式但缺目标：要求澄清，**不猜、不执行**（HA "槽必填" 经验）。
            return RouteHandoff(
                trace=trace,
                route="ambiguous",
                intent="missing_target",
                handling_mode="clarify",
                confidence=0.2,
                slots={},
                required_confirmation="must_clarify",
                tool_scope=(),
                router=_status(),
            )
        return RouteHandoff(
            trace=trace,
            route="task",
            intent="open_application",
            handling_mode="plan_then_execute",
            confidence=0.95,
            slots={
                "application_name": SlotCandidate(
                    value=name,
                    source="utterance",
                    confidence=0.95,
                    confirmed=False,
                )
            },
            required_confirmation="defer_to_safety",
            tool_scope=("start_whitelisted_application",),
            router=_status(),
        )


class TaskFirstRouter:
    """组合路由：本地任务句式优先；未命中才交给 fallback（真实 Provider / 本地聊天路由）。"""

    def __init__(self, primary: RouterLike, fallback: RouterLike) -> None:
        if not callable(getattr(primary, "route", None)):
            raise TypeError("primary 必须有 route()")
        if not callable(getattr(fallback, "route", None)):
            raise TypeError("fallback 必须有 route()")
        self._primary = primary
        self._fallback = fallback

    @property
    def primary(self) -> RouterLike:
        return self._primary

    @property
    def fallback(self) -> RouterLike:
        return self._fallback

    def route(self, request: RouteInput) -> RouteHandoff:
        handoff = self._primary.route(request)
        if handoff is not None:
            return handoff
        # 2026-10-23 真机缺陷：Provider 会把疑问句判成"不支持" ⇒ fallback 结果统一过一遍
        # 疑问/否定兜底（本地 primary 命中不受影响）。
        return downgrade_question_unsupported(self._fallback.route(request), request)


__all__ = [
    "CHAT_HANDLING_MODE",
    "IMPERATIVE_MARKERS",
    "INTENT_CHAT",
    "INTENT_UNSUPPORTED",
    "OP_CLOSE_APPLICATION",
    "OP_DELETE_FILES",
    "QUESTION_FALLBACK_PROVIDER",
    "QUESTION_MARKERS",
    "ROUTE_CHAT",
    "ROUTER_MODEL_ID",
    "ROUTER_MODEL_VERSION",
    "ROUTER_PROVIDER",
    "ROUTE_UNSUPPORTED",
    "STRIP_CHARACTERS",
    "UNSUPPORTED_APP_OBJECTS",
    "UNSUPPORTED_BLOCKERS",
    "UNSUPPORTED_CLOSE_VERBS",
    "UNSUPPORTED_CONFIDENCE",
    "UNSUPPORTED_DELETE_VERBS",
    "UNSUPPORTED_HANDLING_MODE",
    "UNSUPPORTED_PATH_OBJECTS",
    "LocalTaskRouter",
    "RouterLike",
    "TaskFirstRouter",
    "downgrade_question_unsupported",
    "match_application_name",
    "match_unsupported_command",
    "normalize_command",
]
