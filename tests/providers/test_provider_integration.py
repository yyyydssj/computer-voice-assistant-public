"""Offline tests for the explicit provider integration seam."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import ProviderName, load_settings
from app.contracts import ContractError, RouteTrace
from app.providers.deepseek_adapter import DeepSeekProviderAdapter, DeepSeekAdapterConfig
from app.providers.provider_integration import (
    DeepSeekPreflightAssessment,
    DeepSeekPreflightStartupAudit,
    DeepSeekRuntimePreflight,
    ProviderIntegration,
    ProviderIntegrationConfig,
)
from app.routing import RouteInput


class _LocalRouter:
    def __init__(self) -> None:
        self.calls: list[RouteInput] = []

    def route(self, request: RouteInput):
        self.calls.append(request)
        return request


class _Transport:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def send(self, request, *, timeout_seconds: float) -> str:
        self.calls += 1
        return self.response


class ProviderIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        trace = RouteTrace(
            request_id="request-integration",
            conversation_id="conversation-integration",
            turn_id="turn-integration",
            source="text_shell",
            raw_text="打开计算器",
            created_at="2026-09-27T12:00:00Z",
        )
        self.request = RouteInput(raw_text=trace.raw_text, trace=trace)

    @staticmethod
    def deepseek_response() -> str:
        return json.dumps({
            "schema_version": "deepseek-route/v1",
            "status": "success",
            "route": "chat",
            "intent": "general_chat",
            "handling_mode": "chat_reply",
            "confidence": 0.96,
            "slots": {},
            "required_confirmation": "none",
            "tool_scope": [],
        })

    def test_config_accepts_shared_provider_name_type_and_defaults_local(self) -> None:
        default_config = ProviderIntegrationConfig()
        deepseek_config = ProviderIntegrationConfig(provider="deepseek")

        self.assertEqual(default_config.provider, "local")
        self.assertEqual(deepseek_config.provider, "deepseek")
        self.assertIsInstance(default_config.provider, str)
        self.assertIsInstance(deepseek_config.provider, str)
        self.assertEqual(ProviderName.__args__, ("local", "deepseek"))

    def test_default_selection_is_local_and_never_calls_deepseek(self) -> None:
        local = _LocalRouter()
        transport = _Transport(self.deepseek_response())
        adapter = DeepSeekProviderAdapter(
            transport,
            config=DeepSeekAdapterConfig(enabled=True),
        )
        integration = ProviderIntegration(local, deepseek_adapter=adapter)

        result = integration.route(self.request)

        self.assertIs(result, self.request)
        self.assertTrue(integration.provider_ready)
        snapshot = integration.capabilities
        self.assertEqual(snapshot.provider, "local")
        self.assertTrue(snapshot.adapter_injected)
        self.assertFalse(snapshot.adapter_ready)
        self.assertTrue(snapshot.provider_ready)
        self.assertFalse(snapshot.runtime_ready)
        self.assertFalse(snapshot.agent_ready)
        self.assertFalse(snapshot.transport_ready)
        self.assertEqual(snapshot, integration.capability_snapshot)
        self.assertEqual(
            set(snapshot.to_dict()),
            {
                "provider",
                "adapter_injected",
                "adapter_ready",
                "provider_ready",
                "runtime_ready",
                "agent_ready",
                "transport_ready",
            },
        )
        self.assertEqual(local.calls, [self.request])
        self.assertEqual(transport.calls, 0)

    def test_explicit_deepseek_selection_uses_only_injected_adapter(self) -> None:
        local = _LocalRouter()
        transport = _Transport(self.deepseek_response())
        adapter = DeepSeekProviderAdapter(
            transport,
            config=DeepSeekAdapterConfig(enabled=True),
        )
        integration = ProviderIntegration(
            local,
            config=ProviderIntegrationConfig(provider="deepseek"),
            deepseek_adapter=adapter,
        )

        result = integration.route(self.request)

        self.assertEqual(result.route, "chat")
        self.assertEqual(result.router.provider, "deepseek-official")
        self.assertEqual(local.calls, [])
        self.assertEqual(transport.calls, 1)
        self.assertFalse(integration.provider_ready)
        snapshot = integration.capabilities
        self.assertTrue(snapshot.adapter_injected)
        self.assertFalse(snapshot.adapter_ready)
        self.assertFalse(snapshot.provider_ready)
        self.assertFalse(snapshot.runtime_ready)
        self.assertFalse(snapshot.agent_ready)
        self.assertFalse(snapshot.transport_ready)

    def test_deepseek_without_adapter_reports_closed_capabilities(self) -> None:
        integration = ProviderIntegration(
            _LocalRouter(),
            config=ProviderIntegrationConfig(provider="deepseek"),
        )

        snapshot = integration.capabilities

        self.assertEqual(snapshot.provider, "deepseek")
        self.assertFalse(snapshot.adapter_injected)
        self.assertFalse(snapshot.adapter_ready)
        self.assertFalse(snapshot.provider_ready)
        self.assertFalse(snapshot.runtime_ready)
        self.assertFalse(snapshot.agent_ready)
        self.assertFalse(snapshot.transport_ready)

    def test_capability_snapshot_is_immutable_and_projection_is_fresh(self) -> None:
        integration = ProviderIntegration(_LocalRouter())
        snapshot = integration.capabilities

        with self.assertRaises(AttributeError):
            snapshot.runtime_ready = True  # type: ignore[misc]

        projection = snapshot.to_dict()
        projection["runtime_ready"] = True
        self.assertFalse(snapshot.runtime_ready)
        self.assertFalse(integration.capabilities.runtime_ready)
        self.assertNotIn("transport", projection)
        self.assertNotIn("payload", projection)
        self.assertNotIn("DEEPSEEK_API_KEY", projection)

    @staticmethod
    def documented_preflight() -> DeepSeekRuntimePreflight:
        return DeepSeekRuntimePreflight(
            credential_source="process_environment:DEEPSEEK_API_KEY",
            request_limit=1,
            max_cost_usd=0.10,
            rate_limit_per_minute=1,
            transport_implementation="deepseek_https",
            timeout_seconds=10.0,
            retry_limit=0,
            logs_redacted=True,
            rollback_defined=True,
            computer_side_effects_blocked=True,
        )

    def test_preflight_missing_terms_are_closed_and_non_sensitive(self) -> None:
        integration = ProviderIntegration(
            _LocalRouter(), config=ProviderIntegrationConfig(provider="deepseek"),
        )
        assessment = integration.assess_runtime_preflight()

        self.assertFalse(assessment.documentation_complete)
        self.assertFalse(assessment.runtime_allowed)
        self.assertIn("credential_source", assessment.missing_requirements)
        self.assertIn("request_limit", assessment.missing_requirements)
        self.assertIn("retry_limit", assessment.missing_requirements)
        self.assertNotIn("DEEPSEEK_API_KEY", str(assessment))
        self.assertEqual(assessment, integration.assess_runtime_preflight())
        with patch("os.environ.get", side_effect=AssertionError("secret lookup")):
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                self.assertEqual(assessment, integration.assess_runtime_preflight())

    def test_documented_preflight_cannot_promote_capabilities_or_call_transport(self) -> None:
        transport = _Transport(self.deepseek_response())
        adapter = DeepSeekProviderAdapter(
            transport, config=DeepSeekAdapterConfig(enabled=True),
        )
        integration = ProviderIntegration(
            _LocalRouter(), config=ProviderIntegrationConfig(provider="deepseek"),
            deepseek_adapter=adapter,
        )
        preflight = self.documented_preflight()
        assessment = integration.assess_runtime_preflight(preflight)

        self.assertTrue(assessment.documentation_complete)
        self.assertEqual(assessment.missing_requirements, ())
        self.assertFalse(assessment.runtime_allowed)
        self.assertFalse(integration.capabilities.runtime_ready)
        self.assertFalse(integration.capabilities.agent_ready)
        self.assertFalse(integration.capabilities.transport_ready)
        self.assertEqual(transport.calls, 0)
        self.assertNotIn("credential_source", assessment.to_dict())
        self.assertNotIn("DEEPSEEK_API_KEY", str(assessment.to_dict()))
        projection = assessment.to_dict()
        projection["missing_requirements"].append("unexpected")
        self.assertEqual(assessment.missing_requirements, ())
        with self.assertRaises(AttributeError):
            assessment.runtime_allowed = True  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            preflight.request_limit = 2  # type: ignore[misc]

    def test_preflight_consistency_audit_accepts_aligned_fake_contracts(self) -> None:
        transport = _Transport(self.deepseek_response())
        adapter_config = DeepSeekAdapterConfig(
            enabled=True, timeout_seconds=10.0, max_retries=0,
        )
        adapter = DeepSeekProviderAdapter(transport, config=adapter_config)
        integration = ProviderIntegration(
            _LocalRouter(),
            config=ProviderIntegrationConfig(provider="deepseek"),
            deepseek_adapter=adapter,
        )

        audit = integration.audit_runtime_preflight_consistency(
            self.documented_preflight(),
            adapter_config,
            observed_request_count=0,
            status_projection=integration.capabilities.to_dict(),
        )

        self.assertTrue(audit.documentation_complete)
        self.assertTrue(audit.consistent)
        self.assertEqual(audit.mismatches, ())
        self.assertEqual(audit.request_budget_remaining, 1)
        self.assertTrue(audit.status_projection_consistent)
        self.assertFalse(audit.runtime_allowed)
        self.assertEqual(transport.calls, 0)
        self.assertNotIn("DEEPSEEK_API_KEY", str(audit.to_dict()))

    def test_preflight_consistency_audit_reports_timeout_retry_and_budget_mismatches(self) -> None:
        integration = ProviderIntegration(
            _LocalRouter(), config=ProviderIntegrationConfig(provider="deepseek"),
        )
        adapter_config = DeepSeekAdapterConfig(
            enabled=True, timeout_seconds=30.0, max_retries=2,
        )

        audit = integration.audit_runtime_preflight_consistency(
            self.documented_preflight(),
            adapter_config,
            observed_request_count=2,
            status_projection={"runtime_ready": True},
        )

        self.assertFalse(audit.consistent)
        self.assertEqual(
            audit.mismatches,
            (
                "timeout_seconds_mismatch",
                "retry_limit_mismatch",
                "request_budget_exceeded",
                "request_limit_below_retry_attempt_budget",
                "status_projection_mismatch",
            ),
        )
        self.assertEqual(audit.request_budget_remaining, -1)
        self.assertFalse(audit.status_projection_consistent)
        self.assertFalse(audit.runtime_allowed)

    def test_preflight_consistency_audit_rejects_forged_or_invalid_inputs(self) -> None:
        integration = ProviderIntegration(_LocalRouter())
        adapter_config = DeepSeekAdapterConfig()

        with self.assertRaises(ContractError):
            integration.audit_runtime_preflight_consistency(
                object(), adapter_config,  # type: ignore[arg-type]
            )
        with self.assertRaises(ContractError):
            integration.audit_runtime_preflight_consistency(
                self.documented_preflight(), object(),  # type: ignore[arg-type]
            )
        with self.assertRaises(ContractError):
            integration.audit_runtime_preflight_consistency(
                self.documented_preflight(), adapter_config, observed_request_count=-1,
            )
        with self.assertRaises(ContractError):
            integration.audit_runtime_preflight_consistency(
                self.documented_preflight(), adapter_config, status_projection=[],  # type: ignore[arg-type]
            )

    def test_preflight_consistency_audit_is_offline_and_does_not_promote_readiness(self) -> None:
        integration = ProviderIntegration(
            _LocalRouter(), config=ProviderIntegrationConfig(provider="deepseek"),
        )
        with patch("os.environ.get", side_effect=AssertionError("secret lookup")):
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                audit = integration.audit_runtime_preflight_consistency(
                    self.documented_preflight(), DeepSeekAdapterConfig(),
                )

        self.assertFalse(audit.runtime_allowed)
        self.assertFalse(audit.consistent)
        self.assertFalse(integration.capabilities.runtime_ready)
        self.assertFalse(integration.capabilities.agent_ready)
        self.assertFalse(integration.capabilities.transport_ready)

    def test_incomplete_preflight_exposes_only_missing_requirement_names(self) -> None:
        from dataclasses import replace

        preflight = replace(
            self.documented_preflight(), logs_redacted=False,
            computer_side_effects_blocked=False,
        )
        integration = ProviderIntegration(
            _LocalRouter(), config=ProviderIntegrationConfig(provider="deepseek"),
        )
        assessment = integration.assess_runtime_preflight(preflight)

        self.assertFalse(assessment.documentation_complete)
        self.assertEqual(
            assessment.missing_requirements,
            ("logs_redacted", "computer_side_effects_blocked"),
        )
        self.assertFalse(assessment.runtime_allowed)

    def test_invalid_preflight_values_fail_closed(self) -> None:
        from dataclasses import replace

        valid = self.documented_preflight()
        for field, invalid in (
            ("credential_source", "actual-secret"),
            ("transport_implementation", "unreviewed_transport"),
            ("request_limit", True),
            ("request_limit", -1),
            ("max_cost_usd", float("nan")),
            ("rate_limit_per_minute", 0),
            ("timeout_seconds", float("inf")),
            ("retry_limit", -1),
            ("logs_redacted", "yes"),
            ("rollback_defined", 1),
            ("computer_side_effects_blocked", "true"),
        ):
            with self.subTest(field=field, invalid=invalid):
                with self.assertRaises(ContractError):
                    replace(valid, **{field: invalid})

    def test_assessment_cannot_be_forged_into_runtime_permission(self) -> None:
        with self.assertRaises(ContractError):
            DeepSeekPreflightAssessment(True, (), runtime_allowed=True)
        with self.assertRaises(ContractError):
            DeepSeekPreflightAssessment(True, ("request_limit",))
        with self.assertRaises(ContractError):
            ProviderIntegration(_LocalRouter()).assess_runtime_preflight(object())

    def test_local_preflight_is_not_a_deepseek_activation_path(self) -> None:
        integration = ProviderIntegration(_LocalRouter())
        assessment = integration.assess_runtime_preflight(self.documented_preflight())
        self.assertTrue(assessment.documentation_complete)
        self.assertFalse(assessment.runtime_allowed)
        self.assertEqual(integration.capabilities.provider, "local")

    def test_preflight_startup_boundary_accepts_aligned_fake_settings_and_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root.parent / (root.name + "-runtime-data")
            settings = load_settings(
                project_root=root,
                environ={"ASSISTANT_DATA_ROOT": str(data_root)},
            )
        integration = ProviderIntegration(_LocalRouter())
        projection = {
            "mode": "text",
            **integration.capabilities.to_dict(),
            "voice_available": False,
            "task_state": "idle",
        }

        audit = integration.audit_preflight_startup_boundary(
            settings, self.documented_preflight(), projection,
        )

        self.assertTrue(audit.documentation_complete)
        self.assertTrue(audit.consistent)
        self.assertTrue(audit.startup_projection_consistent)
        self.assertTrue(audit.readiness_preserved)
        self.assertEqual(audit.mismatches, ())
        self.assertFalse(audit.runtime_allowed)
        self.assertNotIn("DEEPSEEK_API_KEY", str(audit.to_dict()))

    def test_preflight_startup_boundary_rejects_provider_projection_and_readiness_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root.parent / (root.name + "-runtime-data")
            settings = load_settings(
                project_root=root,
                environ={
                    "ASSISTANT_DATA_ROOT": str(data_root),
                    "ASSISTANT_PROVIDER": "deepseek",
                },
            )
            integration = ProviderIntegration(_LocalRouter())
            projection = integration.capabilities.to_dict()
            projection["runtime_ready"] = True
            projection.pop("transport_ready")

            audit = integration.audit_preflight_startup_boundary(
                settings, self.documented_preflight(), projection,
            )

        self.assertFalse(audit.consistent)
        self.assertFalse(audit.startup_projection_consistent)
        self.assertFalse(audit.readiness_preserved)
        self.assertIn("settings_provider_mismatch", audit.mismatches)
        self.assertIn("startup_projection_mismatch", audit.mismatches)
        self.assertIn("readiness_promotion", audit.mismatches)
        self.assertFalse(audit.runtime_allowed)
        self.assertFalse(integration.capabilities.runtime_ready)
        self.assertFalse(integration.capabilities.agent_ready)
        self.assertFalse(integration.capabilities.transport_ready)

    def test_preflight_startup_boundary_is_offline_and_rejects_forged_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root.parent / (root.name + "-runtime-data")
            settings = load_settings(
                project_root=root,
                environ={"ASSISTANT_DATA_ROOT": str(data_root)},
            )
        integration = ProviderIntegration(_LocalRouter())
        with patch("os.environ.get", side_effect=AssertionError("secret lookup")):
            with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                audit = integration.audit_preflight_startup_boundary(
                    settings, self.documented_preflight(),
                    integration.capabilities.to_dict(),
                )
        self.assertTrue(audit.consistent)
        self.assertFalse(audit.runtime_allowed)
        with self.assertRaises(ContractError):
            integration.audit_preflight_startup_boundary(
                object(), self.documented_preflight(), {},  # type: ignore[arg-type]
            )
        with self.assertRaises(ContractError):
            integration.audit_preflight_startup_boundary(
                settings, object(), {},  # type: ignore[arg-type]
            )
        with self.assertRaises(ContractError):
            integration.audit_preflight_startup_boundary(
                settings, self.documented_preflight(), object(),  # type: ignore[arg-type]
            )
        with self.assertRaises(ContractError):
            DeepSeekPreflightStartupAudit(
                True, True, (), True, True, runtime_allowed=True,
            )

    def test_deepseek_selection_requires_explicit_adapter(self) -> None:
        integration = ProviderIntegration(
            _LocalRouter(),
            config=ProviderIntegrationConfig(provider="deepseek"),
        )

        with self.assertRaises(ContractError):
            integration.route(self.request)

    def test_invalid_selection_and_request_fail_closed_at_boundary(self) -> None:
        with self.assertRaises(ContractError):
            ProviderIntegrationConfig(provider="unknown")  # type: ignore[arg-type]

        integration = ProviderIntegration(_LocalRouter())
        with self.assertRaises(ContractError):
            integration.route(object())  # type: ignore[arg-type]

    def test_deepseek_failure_remains_non_executable(self) -> None:
        transport = _Transport(json.dumps({
            "schema_version": "deepseek-route/v1",
            "status": "invalid_response",
        }))
        adapter = DeepSeekProviderAdapter(
            transport,
            config=DeepSeekAdapterConfig(enabled=True),
        )
        integration = ProviderIntegration(
            _LocalRouter(),
            config=ProviderIntegrationConfig(provider="deepseek"),
            deepseek_adapter=adapter,
        )

        result = integration.route(self.request)

        self.assertEqual(result.route, "ambiguous")
        self.assertEqual(result.intent, "invalid_router_output")
        self.assertEqual(result.slots, {})
        self.assertEqual(result.tool_scope, ())


if __name__ == "__main__":
    unittest.main()
