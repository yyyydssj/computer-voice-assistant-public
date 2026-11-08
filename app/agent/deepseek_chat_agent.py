"""DeepSeek 真实对话 Agent 适配器（非流式、单轮）。

把已校验的对话传输接到既有 Agent 契约（``submit_chat -> AgentTurnOutcome``）上：

- 默认关闭：``enabled`` 与 ``authorized`` 皆由配置显式打开；
- **非流式**：本实现不会调用 ``on_text_chunk``（保留参数以兼容既有调用方）；
- **单轮**：不带会话历史，每次提交只发送当前用户文本；
- 失败一律映射为 ``AgentFailureCategory`` 枚举并如实返回，绝不伪造回复、
  不自动重试、不回退脚本替身；
- 不接触工具、计划、授权、账本、执行器或验证链。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from app.agent.adapter import AgentFailure, AgentFailureCategory, AgentTurnOutcome
from app.contracts.base import require_identifier, require_non_empty
from app.events import RequestSubmissionGate
from app.providers.deepseek_chat_transport import (
    DeepSeekChatConfig,
    DeepSeekChatError,
    DeepSeekChatRateLimited,
    DeepSeekChatRequest,
    DeepSeekChatUnavailable,
)


class DeepSeekChatAgentAdapter:
    """默认关闭的真实对话适配器；就绪与否由配置显式声明。"""

    def __init__(
        self,
        transport: object,
        *,
        config: DeepSeekChatConfig | None = None,
    ) -> None:
        if not callable(getattr(transport, "send", None)):
            raise TypeError("DeepSeek chat transport must expose send()")
        self._transport = transport
        self._config = config or DeepSeekChatConfig()
        self._request_gate = RequestSubmissionGate()
        self._closed = False

    @property
    def config(self) -> DeepSeekChatConfig:
        return self._config

    @property
    def provider_ready(self) -> bool:
        """只有显式 ``enabled`` 且 ``authorized`` 时才声明就绪。"""

        return bool(self._config.enabled and self._config.authorized)

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
        if not text.strip():
            raise ValueError("text 不能是纯空白")
        if not self._request_gate.submit_once(request_id):
            raise ValueError("request_id 已经提交，不能自动重试或重复提交")
        if self._closed:
            return self._failure(
                request_id,
                session_id,
                AgentFailureCategory.CONNECTION,
                "closed",
            )
        if not self.provider_ready:
            return self._failure(
                request_id,
                session_id,
                AgentFailureCategory.CONNECTION,
                "config",
            )

        request = DeepSeekChatRequest.from_config(self._config, text)
        try:
            content = self._transport.send(  # type: ignore[attr-defined]
                request,
                timeout_seconds=self._config.timeout,
            )
        except TimeoutError:
            return self._failure(request_id, session_id, AgentFailureCategory.TIMEOUT)
        except DeepSeekChatRateLimited:
            return self._failure(request_id, session_id, AgentFailureCategory.RATE_LIMITED)
        except (DeepSeekChatUnavailable, ConnectionError, OSError):
            return self._failure(request_id, session_id, AgentFailureCategory.CONNECTION)
        except (ValueError, DeepSeekChatError):
            return self._failure(request_id, session_id, AgentFailureCategory.SCHEMA_ERROR)
        except Exception:
            return self._failure(request_id, session_id, AgentFailureCategory.UNKNOWN_EVENT)

        if not isinstance(content, str) or not content.strip():
            return self._failure(request_id, session_id, AgentFailureCategory.EMPTY_RESPONSE)
        return AgentTurnOutcome(
            request_id=request_id,
            session_id=session_id,
            completed=True,
            text=content,
            finish_reason="stop",
        )

    def close(self) -> None:
        self._closed = True

    @staticmethod
    def _failure(
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
                diagnostic_id=f"deepseek-chat-{uuid.uuid4().hex}",
            ),
        )


__all__ = ["DeepSeekChatAgentAdapter"]
