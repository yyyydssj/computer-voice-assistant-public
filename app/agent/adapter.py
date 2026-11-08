"""P3 DeepSeekHarness 纯文字适配器。"""

from __future__ import annotations

import uuid
import inspect
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from app.contracts.base import require_identifier, require_non_empty
from app.events import RequestSubmissionGate


class HarnessConfigurationError(RuntimeError):
    """Harness 配置缺失或会扩大 P3 权限。"""


class AgentFailureCategory(str, Enum):
    CONNECTION = "connection"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    STREAM_INTERRUPTED = "stream_interrupted"
    EMPTY_RESPONSE = "empty_response"
    UNKNOWN_EVENT = "unknown_event"
    SCHEMA_ERROR = "schema_error"


@dataclass(frozen=True, slots=True)
class AgentFailure:
    category: AgentFailureCategory
    stage: str
    diagnostic_id: str
    retry_allowed_automatically: bool = False
    alternate_agent_allowed_automatically: bool = False


@dataclass(frozen=True, slots=True)
class AgentTurnOutcome:
    request_id: str
    session_id: str
    completed: bool
    text: str = ""
    finish_reason: str | None = None
    failure: AgentFailure | None = None


@dataclass(frozen=True, slots=True)
class HarnessAdapterConfig:
    workspace: Path
    dsh_home: Path
    safety_patch: Path
    provider: str
    model: str
    system_prompt: str
    initialize_timeout_seconds: float = 30.0
    request_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        workspace = _absolute_local_directory("workspace", self.workspace, must_exist=True)
        dsh_home = _absolute_local_directory("dsh_home", self.dsh_home, must_exist=False)
        safety_patch = Path(self.safety_patch).expanduser()
        if not safety_patch.is_absolute() or not safety_patch.is_file():
            raise HarnessConfigurationError("P3 安全补丁必须是已存在的本地绝对路径文件")
        require_non_empty("provider", self.provider, maximum=128)
        require_non_empty("model", self.model, maximum=128)
        require_non_empty("system_prompt", self.system_prompt, maximum=131072)
        if self.initialize_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise HarnessConfigurationError("Harness 超时必须是正数")
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "dsh_home", dsh_home)
        object.__setattr__(self, "safety_patch", safety_patch.resolve())


class _RunResult(Protocol):
    final_response: str
    finish_reason: str | None
    events: list[object]
    notifications: list[object]


class _HarnessRuntime(Protocol):
    def run(self, text: str, *, session_id: str, on_notification: Callable[[object], None] | None = None) -> _RunResult: ...

    def close(self) -> None: ...


SdkFactory = Callable[[HarnessAdapterConfig], _HarnessRuntime]


_REQUIRED_DISABLED_ROWS = frozenset(
    {
        "session-log-deepseek",
        "plugin-package-inventory-deepseek",
        "sandbox",
        "sandbox-policy",
        "subprocess",
        "pty",
        "terminal-bash",
        "terminal-pwsh",
        "session-title",
        "mcp-resources",
        "llm-retry",
        "jobs",
        "persistent-bash",
        "persistent-pwsh",
        "sessions",
    }
)


class HarnessAdapter:
    """延迟创建官方 SDK runtime，并在本地强制 P3 安全边界。"""

    def __init__(
        self,
        config: HarnessAdapterConfig,
        *,
        sdk_factory: SdkFactory | None = None,
    ) -> None:
        self.config = config
        _validate_no_tools_patch(config.safety_patch)
        self._sdk_factory = sdk_factory or _official_sdk_factory
        self._runtime: _HarnessRuntime | None = None
        self._request_gate = RequestSubmissionGate()
        self._session_ids: set[str] = set()

    @property
    def runtime_created(self) -> bool:
        return self._runtime is not None

    @property
    def session_ids(self) -> frozenset[str]:
        return frozenset(self._session_ids)

    def submit_chat(
        self,
        *,
        request_id: str,
        session_id: str,
        text: str,
        on_text_chunk: Callable[[str], None] | None = None,
    ) -> AgentTurnOutcome:
        require_identifier("request_id", request_id)
        require_identifier("session_id", session_id)
        require_non_empty("text", text, maximum=32768)
        if not self._request_gate.submit_once(request_id):
            raise ValueError("request_id 已经提交，不能自动重试或重复提交")

        try:
            runtime = self._ensure_runtime()
        except HarnessConfigurationError:
            raise
        except Exception as exc:
            return self._failure_outcome(
                request_id=request_id,
                session_id=session_id,
                category=_map_exception(exc),
                stage="initialize",
            )
        self._session_ids.add(session_id)
        try:
            result = self._run_runtime(
                runtime, text=text, session_id=session_id, on_text_chunk=on_text_chunk
            )
        except Exception as exc:
            return self._failure_outcome(
                request_id=request_id,
                session_id=session_id,
                category=_map_exception(exc),
            )

        try:
            response_text = result.final_response
            finish_reason = result.finish_reason
            events = result.events
            notifications = result.notifications
        except (AttributeError, TypeError, ValueError):
            return self._failure_outcome(
                request_id=request_id,
                session_id=session_id,
                category=AgentFailureCategory.SCHEMA_ERROR,
            )
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            return self._failure_outcome(
                request_id=request_id,
                session_id=session_id,
                category=AgentFailureCategory.SCHEMA_ERROR,
            )
        if not isinstance(notifications, list):
            return self._failure_outcome(
                request_id=request_id,
                session_id=session_id,
                category=AgentFailureCategory.SCHEMA_ERROR,
            )
        if not isinstance(response_text, str):
            return self._failure_outcome(
                request_id=request_id,
                session_id=session_id,
                category=AgentFailureCategory.SCHEMA_ERROR,
            )
        if not response_text.strip():
            return self._failure_outcome(
                request_id=request_id,
                session_id=session_id,
                category=AgentFailureCategory.EMPTY_RESPONSE,
            )
        if not isinstance(finish_reason, str) or not finish_reason.strip():
            return self._failure_outcome(
                request_id=request_id,
                session_id=session_id,
                category=AgentFailureCategory.STREAM_INTERRUPTED,
            )
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=response_text,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _run_runtime(
        runtime: _HarnessRuntime,
        *,
        text: str,
        session_id: str,
        on_text_chunk: Callable[[str], None] | None,
    ) -> _RunResult:
        if on_text_chunk is None:
            return runtime.run(text, session_id=session_id)

        def on_notification(notification: object) -> None:
            chunk = _extract_text_chunk(notification)
            if chunk:
                on_text_chunk(chunk)

        # Keep old injected test doubles compatible while avoiding a second
        # provider submission if a runtime raises an unrelated TypeError.
        try:
            parameters = inspect.signature(runtime.run).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "on_notification" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return runtime.run(text, session_id=session_id, on_notification=on_notification)
        return runtime.run(text, session_id=session_id)

    def close(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        self._runtime = None
        self._session_ids.clear()
        runtime.close()

    def _ensure_runtime(self) -> _HarnessRuntime:
        if self._runtime is None:
            self.config.dsh_home.mkdir(parents=True, exist_ok=True)
            self._runtime = self._sdk_factory(self.config)
        return self._runtime

    @staticmethod
    def _failure_outcome(
        *,
        request_id: str,
        session_id: str,
        category: AgentFailureCategory,
        stage: str = "request",
    ) -> AgentTurnOutcome:
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=False,
            failure=AgentFailure(
                category=category,
                stage=stage,
                diagnostic_id=f"agent-{uuid.uuid4().hex}",
            ),
        )


def _absolute_local_directory(name: str, value: Path, *, must_exist: bool) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HarnessConfigurationError(f"{name} 必须是本地绝对路径")
    text = str(path)
    if text.startswith("\\\\") or path.drive == "":
        raise HarnessConfigurationError(f"{name} 不得使用 UNC 或网络路径")
    resolved = path.resolve(strict=must_exist)
    if must_exist and not resolved.is_dir():
        raise HarnessConfigurationError(f"{name} 必须是已存在目录")
    if not must_exist and resolved.exists() and not resolved.is_dir():
        raise HarnessConfigurationError(f"{name} 不能是文件")
    return resolved


def _validate_no_tools_patch(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise HarnessConfigurationError("P3 安全补丁不可读取或不是严格 UTF-8") from exc

    disabled_rows: set[str] = set()
    current_id: str | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("- id:"):
            current_id = stripped.partition(":")[2].strip().strip("'\"")
            continue
        if current_id is not None and stripped == "disabled: true":
            disabled_rows.add(current_id)
            current_id = None
    missing = sorted(_REQUIRED_DISABLED_ROWS - disabled_rows)
    if missing:
        raise HarnessConfigurationError(
            "P3 安全补丁缺少禁用项，拒绝启动 Harness: " + ", ".join(missing)
        )



def _extract_text_chunk(notification: object) -> str:
    """Extract only assistant text-chunks from an SDK session notification."""
    method = getattr(notification, "method", None)
    payload = getattr(notification, "payload", None)
    if isinstance(notification, dict):
        method = notification.get("method")
        payload = notification.get("payload")
    if method != "session.event" or not isinstance(payload, dict):
        return ""
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "assistant/message":
        return ""
    data = event.get("data")
    message = data.get("message") if isinstance(data, dict) else None
    stream = message.get("stream") if isinstance(message, dict) else None
    if not isinstance(stream, dict) or stream.get("type") != "text-chunks":
        return ""
    texts = stream.get("texts")
    if not isinstance(texts, list):
        return ""
    return "".join(item for item in texts if isinstance(item, str))

def _official_sdk_factory(config: HarnessAdapterConfig) -> _HarnessRuntime:
    try:
        from deepseek_harness import DeepSeekHarness
    except ImportError as exc:
        raise HarnessConfigurationError(
            "未安装官方 deepseek-harness-sdk，不能创建 Harness 会话"
        ) from exc

    return DeepSeekHarness(
        provider=config.provider,
        model=config.model,
        cwd=str(config.workspace),
        runtime_cwd=str(config.workspace),
        dsh_home=str(config.dsh_home),
        profile="sdk-minimal",
        patches=(str(config.safety_patch),),
        env={"DSH_SYSTEM_PROMPT": config.system_prompt},
        initialize_timeout_seconds=config.initialize_timeout_seconds,
        request_timeout_seconds=config.request_timeout_seconds,
    )


def _map_exception(exc: BaseException) -> AgentFailureCategory:
    if isinstance(exc, TimeoutError):
        return AgentFailureCategory.TIMEOUT
    class_name = type(exc).__name__
    if class_name == "TransportClosedError":
        return AgentFailureCategory.STREAM_INTERRUPTED
    if class_name == "SdkProtocolError":
        return AgentFailureCategory.UNKNOWN_EVENT
    if class_name == "JsonRpcError":
        code = getattr(exc, "code", None)
        message = str(getattr(exc, "message", exc)).casefold()
        if code == 429 or "rate limit" in message or "限流" in message:
            return AgentFailureCategory.RATE_LIMITED
        if code in {-32600, -32601, -32602} or "schema" in message or "invalid" in message:
            return AgentFailureCategory.SCHEMA_ERROR
        return AgentFailureCategory.CONNECTION
    if isinstance(exc, (ConnectionError, OSError)):
        return AgentFailureCategory.CONNECTION
    return AgentFailureCategory.UNKNOWN_EVENT
