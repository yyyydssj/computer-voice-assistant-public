"""Explicit, default-local provider integration seam.

This module selects an already-built routing provider without changing the
runtime entrypoint.  The default selection is local and never constructs or
calls a DeepSeek transport.  DeepSeek remains an explicitly injected,
provider-specific option for offline integration tests and separately
authorized experiments.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from app.config import ProviderName, Settings
from app.contracts.base import ContractError
from app.contracts.routing import RouteHandoff
from app.routing import RouteInput
from app.providers.deepseek_adapter import DeepSeekAdapterConfig, DeepSeekProviderAdapter


@dataclass(frozen=True, slots=True)
class ProviderCapabilitySnapshot:
    """Non-sensitive provider state; runtime and Agent stay opt-in and false."""

    provider: ProviderName
    adapter_injected: bool
    adapter_ready: bool
    provider_ready: bool
    runtime_ready: bool
    agent_ready: bool
    transport_ready: bool

    def to_dict(self) -> dict[str, object]:
        """Return a fresh, log-safe projection without internals or payloads."""

        return {
            "provider": self.provider,
            "adapter_injected": self.adapter_injected,
            "adapter_ready": self.adapter_ready,
            "provider_ready": self.provider_ready,
            "runtime_ready": self.runtime_ready,
            "agent_ready": self.agent_ready,
            "transport_ready": self.transport_ready,
        }


# Allowlisted labels describe a source/implementation, never carry a credential.
_CREDENTIAL_SOURCE = "process_environment:DEEPSEEK_API_KEY"
_TRANSPORT_IMPLEMENTATION = "deepseek_https"


@dataclass(frozen=True, slots=True)
class DeepSeekRuntimePreflight:
    """Offline checklist only. Construction does not authorize network access."""

    credential_source: str | None = field(default=None, repr=False)
    request_limit: int | None = None
    max_cost_usd: float | None = None
    rate_limit_per_minute: int | None = None
    transport_implementation: str | None = None
    timeout_seconds: float | None = None
    retry_limit: int | None = None
    logs_redacted: bool = False
    rollback_defined: bool = False
    computer_side_effects_blocked: bool = False

    def __post_init__(self) -> None:
        if self.credential_source not in (None, _CREDENTIAL_SOURCE):
            raise ContractError("credential source must be the approved environment label")
        if self.transport_implementation not in (None, _TRANSPORT_IMPLEMENTATION):
            raise ContractError("transport implementation must be the reviewed label")
        for name in ("request_limit", "rate_limit_per_minute"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ContractError(f"{name} must be a positive integer")
        if self.retry_limit is not None and (type(self.retry_limit) is not int or self.retry_limit < 0):
            raise ContractError("retry_limit must be a non-negative integer")
        for name in ("max_cost_usd", "timeout_seconds"):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in (float, int) or not math.isfinite(value) or value <= 0
            ):
                raise ContractError(f"{name} must be positive and finite")
        for name in ("logs_redacted", "rollback_defined", "computer_side_effects_blocked"):
            if type(getattr(self, name)) is not bool:
                raise ContractError(f"{name} must be a bool")


@dataclass(frozen=True, slots=True)
class DeepSeekPreflightAssessment:
    """Log-safe result: checklist completeness is never runtime permission."""

    documentation_complete: bool
    missing_requirements: tuple[str, ...]
    runtime_allowed: bool = False

    def __post_init__(self) -> None:
        if type(self.documentation_complete) is not bool or type(self.runtime_allowed) is not bool:
            raise ContractError("preflight assessment flags must be bool")
        if self.runtime_allowed:
            raise ContractError("offline preflight cannot allow runtime access")
        if (
            not isinstance(self.missing_requirements, tuple)
            or any(type(name) is not str for name in self.missing_requirements)
        ):
            raise ContractError("missing_requirements must be a tuple of names")
        if self.documentation_complete != (not self.missing_requirements):
            raise ContractError("preflight completeness must agree with missing requirements")

    def to_dict(self) -> dict[str, object]:
        return {
            "documentation_complete": self.documentation_complete,
            "missing_requirements": list(self.missing_requirements),
            "runtime_allowed": self.runtime_allowed,
        }


@dataclass(frozen=True, slots=True)
class DeepSeekPreflightConsistencyAudit:
    """Offline comparison between declared preflight terms and fake config facts."""

    documentation_complete: bool
    consistent: bool
    mismatches: tuple[str, ...]
    observed_request_count: int
    request_budget_remaining: int
    status_projection_consistent: bool
    runtime_allowed: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.documentation_complete) is not bool
            or type(self.consistent) is not bool
            or type(self.status_projection_consistent) is not bool
            or type(self.runtime_allowed) is not bool
        ):
            raise ContractError("preflight audit flags must be bool")
        if self.runtime_allowed:
            raise ContractError("offline preflight audit cannot allow runtime access")
        if (
            not isinstance(self.mismatches, tuple)
            or any(type(name) is not str or not name for name in self.mismatches)
        ):
            raise ContractError("preflight audit mismatches must be a tuple of names")
        if self.consistent != (not self.mismatches):
            raise ContractError("preflight audit consistency must agree with mismatches")
        if type(self.observed_request_count) is not int or self.observed_request_count < 0:
            raise ContractError("observed_request_count must be a non-negative integer")
        if type(self.request_budget_remaining) is not int:
            raise ContractError("request_budget_remaining must be an integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "documentation_complete": self.documentation_complete,
            "consistent": self.consistent,
            "mismatches": list(self.mismatches),
            "observed_request_count": self.observed_request_count,
            "request_budget_remaining": self.request_budget_remaining,
            "status_projection_consistent": self.status_projection_consistent,
            "runtime_allowed": self.runtime_allowed,
        }


@dataclass(frozen=True, slots=True)
class DeepSeekPreflightStartupAudit:
    """Offline audit of the preflight-to-startup status boundary."""

    documentation_complete: bool
    consistent: bool
    mismatches: tuple[str, ...]
    startup_projection_consistent: bool
    readiness_preserved: bool
    runtime_allowed: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.documentation_complete) is not bool
            or type(self.consistent) is not bool
            or type(self.startup_projection_consistent) is not bool
            or type(self.readiness_preserved) is not bool
            or type(self.runtime_allowed) is not bool
        ):
            raise ContractError("preflight startup audit flags must be bool")
        if self.runtime_allowed:
            raise ContractError("offline preflight startup audit cannot allow runtime access")
        if (
            not isinstance(self.mismatches, tuple)
            or any(type(name) is not str or not name for name in self.mismatches)
        ):
            raise ContractError("preflight startup audit mismatches must be a tuple of names")
        if self.consistent != (not self.mismatches):
            raise ContractError("preflight startup audit consistency must agree with mismatches")

    def to_dict(self) -> dict[str, object]:
        return {
            "documentation_complete": self.documentation_complete,
            "consistent": self.consistent,
            "mismatches": list(self.mismatches),
            "startup_projection_consistent": self.startup_projection_consistent,
            "readiness_preserved": self.readiness_preserved,
            "runtime_allowed": self.runtime_allowed,
        }


@dataclass(frozen=True, slots=True)
class ProviderIntegrationConfig:
    """Validated provider selection; local routing is the safe default."""

    provider: ProviderName = "local"

    def __post_init__(self) -> None:
        if self.provider not in ("local", "deepseek"):
            raise ContractError("provider selection must be local or deepseek")


class ProviderIntegration:
    """Route through an explicitly selected provider with no fallback logic."""

    def __init__(
        self,
        local_router: IntentRouter,
        *,
        config: ProviderIntegrationConfig | None = None,
        deepseek_adapter: DeepSeekProviderAdapter | None = None,
        deepseek_transport_ready: bool = False,
        agent_ready: bool = False,
    ) -> None:
        if not callable(getattr(local_router, "route", None)):
            raise TypeError("local_router must expose route()")
        if deepseek_adapter is not None and not callable(getattr(deepseek_adapter, "route", None)):
            raise TypeError("deepseek_adapter must expose route()")
        if type(deepseek_transport_ready) is not bool:
            raise ContractError("deepseek_transport_ready must be a bool")
        if deepseek_transport_ready and deepseek_adapter is None:
            raise ContractError("transport readiness requires an injected adapter")
        if type(agent_ready) is not bool:
            raise ContractError("agent_ready must be a bool")
        if agent_ready and not deepseek_transport_ready:
            raise ContractError("agent readiness requires transport readiness")
        self._local_router = local_router
        self._config = config or ProviderIntegrationConfig()
        self._deepseek_adapter = deepseek_adapter
        self._deepseek_transport_ready = deepseek_transport_ready
        self._agent_ready = agent_ready

    @property
    def config(self) -> ProviderIntegrationConfig:
        return self._config

    @property
    def provider_ready(self) -> bool:
        """Expose provider-seam readiness without promoting it to runtime use."""

        if self._config.provider == "local":
            return True
        return bool(self._deepseek_adapter and self._deepseek_adapter.provider_ready)

    @property
    def capabilities(self) -> ProviderCapabilitySnapshot:
        """Return a side-effect-free snapshot of the current integration boundary.

        语义（批次 A/B 之后）：

        - ``transport_ready``：真实 HTTPS transport 已装配且被授权（仅路由层）；
        - ``agent_ready``：真实对话 Agent 已接通（由调用方显式声明，且要求 transport 就绪）；
        - ``runtime_ready``：端到端路径接通，即 transport 与 agent 同时就绪。

        本地默认路径三者皆为 ``False``，行为与既有一致。
        """

        adapter_injected = self._deepseek_adapter is not None
        adapter_ready = bool(
            adapter_injected
            and getattr(self._deepseek_adapter, "provider_ready", False)
        )
        transport_ready = bool(
            self._config.provider == "deepseek"
            and adapter_injected
            and self._deepseek_transport_ready
        )
        agent_ready = bool(self._agent_ready and transport_ready)
        return ProviderCapabilitySnapshot(
            provider=self._config.provider,
            adapter_injected=adapter_injected,
            adapter_ready=adapter_ready,
            provider_ready=self.provider_ready,
            runtime_ready=bool(transport_ready and agent_ready),
            agent_ready=agent_ready,
            transport_ready=transport_ready,
        )

    @property
    def capability_snapshot(self) -> ProviderCapabilitySnapshot:
        """Explicit alias for callers that want the snapshot terminology."""

        return self.capabilities

    @property
    def router_metadata(self) -> tuple[str, str, str]:
        """返回该边界对应路由器的 ``(provider, model_id, model_version)`` 元数据。

        本地选择返回本地占位标识；DeepSeek 选择返回适配器配置中的真实标识，
        供 ConversationManager 构造失败交接时使用（不泄露任何凭据）。
        """

        if self._config.provider == "deepseek" and self._deepseek_adapter is not None:
            config = self._deepseek_adapter.config
            return (config.provider_name, config.model_id, config.model_version)
        return ("local", "chat-only-router", "1")

    def assess_runtime_preflight(
        self, preflight: DeepSeekRuntimePreflight | None = None,
    ) -> DeepSeekPreflightAssessment:
        """Assess documented terms without inspecting secrets or invoking a provider.

        Even a complete checklist cannot grant runtime/Agent/network access.
        """
        if preflight is None:
            preflight = DeepSeekRuntimePreflight()
        if not isinstance(preflight, DeepSeekRuntimePreflight):
            raise ContractError("preflight must be a DeepSeekRuntimePreflight")
        missing = tuple(
            name for name in (
                "credential_source", "request_limit", "max_cost_usd",
                "rate_limit_per_minute", "transport_implementation",
                "timeout_seconds", "retry_limit", "logs_redacted",
                "rollback_defined", "computer_side_effects_blocked",
            )
            if getattr(preflight, name) is None
            or (name in (
                "logs_redacted", "rollback_defined", "computer_side_effects_blocked",
            ) and not getattr(preflight, name))
        )
        return DeepSeekPreflightAssessment(
            documentation_complete=not missing,
            missing_requirements=missing,
        )

    def audit_runtime_preflight_consistency(
        self,
        preflight: DeepSeekRuntimePreflight,
        adapter_config: DeepSeekAdapterConfig,
        *,
        observed_request_count: int = 0,
        status_projection: Mapping[str, object] | None = None,
    ) -> DeepSeekPreflightConsistencyAudit:
        """Compare declarations with adapter config and a fake observed projection.

        This audit is deliberately inert: it accepts only already-built config
        values and fake counters, and it never reads credentials, constructs a
        transport, invokes routing, or changes any readiness flag.
        """
        if not isinstance(preflight, DeepSeekRuntimePreflight):
            raise ContractError("preflight must be a DeepSeekRuntimePreflight")
        if not isinstance(adapter_config, DeepSeekAdapterConfig):
            raise ContractError("adapter_config must be a DeepSeekAdapterConfig")
        if type(observed_request_count) is not int or observed_request_count < 0:
            raise ContractError("observed_request_count must be a non-negative integer")
        if status_projection is not None and not isinstance(status_projection, Mapping):
            raise ContractError("status_projection must be a mapping")

        assessment = self.assess_runtime_preflight(preflight)
        mismatches = list(assessment.missing_requirements)

        def add(name: str) -> None:
            if name not in mismatches:
                mismatches.append(name)

        if preflight.transport_implementation != _TRANSPORT_IMPLEMENTATION:
            add("transport_implementation_mismatch")
        if (
            preflight.timeout_seconds is not None
            and float(preflight.timeout_seconds) != adapter_config.timeout
        ):
            add("timeout_seconds_mismatch")
        if (
            preflight.retry_limit is not None
            and preflight.retry_limit != adapter_config.max_retries
        ):
            add("retry_limit_mismatch")

        request_budget_remaining = -1
        if preflight.request_limit is not None:
            request_budget_remaining = preflight.request_limit - observed_request_count
            if observed_request_count > preflight.request_limit:
                add("request_budget_exceeded")
            if preflight.request_limit < adapter_config.max_retries + 1:
                add("request_limit_below_retry_attempt_budget")

        expected_projection = self.capabilities.to_dict()
        actual_projection = (
            expected_projection
            if status_projection is None
            else dict(status_projection)
        )
        status_projection_consistent = actual_projection == expected_projection
        if not status_projection_consistent:
            add("status_projection_mismatch")

        return DeepSeekPreflightConsistencyAudit(
            documentation_complete=assessment.documentation_complete,
            consistent=not mismatches,
            mismatches=tuple(mismatches),
            observed_request_count=observed_request_count,
            request_budget_remaining=request_budget_remaining,
            status_projection_consistent=status_projection_consistent,
        )

    def audit_preflight_startup_boundary(
        self,
        settings: Settings,
        preflight: DeepSeekRuntimePreflight,
        startup_projection: Mapping[str, object],
    ) -> DeepSeekPreflightStartupAudit:
        """Audit fake Settings and startup status without entering runtime paths.

        The projection may contain startup-only fields such as ``mode`` or
        ``task_state``. Only immutable capability fields are compared. This
        method never loads settings, reads secrets, creates transports, routes
        requests, or promotes any readiness flag.
        """
        if not isinstance(settings, Settings):
            raise ContractError("settings must be Settings")
        if not isinstance(preflight, DeepSeekRuntimePreflight):
            raise ContractError("preflight must be a DeepSeekRuntimePreflight")
        if not isinstance(startup_projection, Mapping):
            raise ContractError("startup_projection must be a mapping")

        assessment = self.assess_runtime_preflight(preflight)
        mismatches = list(assessment.missing_requirements)

        def add(name: str) -> None:
            if name not in mismatches:
                mismatches.append(name)

        if settings.provider != self._config.provider:
            add("settings_provider_mismatch")

        expected_projection = self.capabilities.to_dict()
        missing_fields = tuple(
            name for name in expected_projection if name not in startup_projection
        )
        projection_values_match = all(
            startup_projection.get(name) == value
            for name, value in expected_projection.items()
            if name in startup_projection
        )
        startup_projection_consistent = not missing_fields and projection_values_match
        if not startup_projection_consistent:
            add("startup_projection_mismatch")

        # 就绪字段不再硬性要求为 False（transport 与 agent 已可被显式授权接通），
        # 而是要求启动投影与真实能力完全一致：任何未授权的提升都会被记为 mismatch。
        readiness_fields = ("runtime_ready", "agent_ready", "transport_ready")
        readiness_preserved = all(
            startup_projection.get(name) == expected_projection[name]
            for name in readiness_fields
        )
        if not readiness_preserved:
            add("readiness_promotion")

        return DeepSeekPreflightStartupAudit(
            documentation_complete=assessment.documentation_complete,
            consistent=not mismatches,
            mismatches=tuple(mismatches),
            startup_projection_consistent=startup_projection_consistent,
            readiness_preserved=readiness_preserved,
        )

    def route(self, request: RouteInput) -> RouteHandoff:
        if not isinstance(request, RouteInput):
            raise ContractError("provider integration request must be RouteInput")
        if self._config.provider == "local":
            return self._local_router.route(request)
        if self._deepseek_adapter is None:
            raise ContractError("deepseek provider requires an injected adapter")
        return self._deepseek_adapter.route(request)


__all__ = [
    "ProviderCapabilitySnapshot",
    "DeepSeekRuntimePreflight",
    "DeepSeekPreflightAssessment",
    "DeepSeekPreflightStartupAudit",
    "ProviderIntegration",
    "ProviderIntegrationConfig",
    "ProviderName",
]
