"""批次 A 运行时门禁测试：双条件开关、transport 就绪语义与不 fallback。

全部离线：构造 provider 边界不读密钥、不联网；真实请求路径不在本文件覆盖范围。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config.settings import ConfigError, Settings, load_settings
from app.contracts import ContractError, RouteTrace
from app.main import (
    build_agent_adapter,
    build_provider_integration,
    provider_startup_notice,
    select_runtime_router,
)
from app.providers.deepseek_adapter import (
    DeepSeekAdapterConfig,
    DeepSeekProviderAdapter,
    DeepSeekRequest,
)
from app.providers.provider_integration import (
    ProviderIntegration,
    ProviderIntegrationConfig,
)
from app.routing import RouteInput


class FakeTransport:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request: DeepSeekRequest, *, timeout_seconds: float) -> str:
        self.calls += 1
        return "{}"


class LocalRouter:
    def __init__(self) -> None:
        self.calls = 0

    def route(self, request: RouteInput):
        self.calls += 1

        class _Sentinel:
            route = "chat"

        return _Sentinel()


def make_adapter(**config_changes: object) -> DeepSeekProviderAdapter:
    values: dict[str, object] = {"enabled": True, "authorized": True}
    values.update(config_changes)
    config = DeepSeekAdapterConfig(**values)  # type: ignore[arg-type]
    return DeepSeekProviderAdapter(FakeTransport(), config=config)


class AdapterReadinessGateTests(unittest.TestCase):
    def test_default_adapter_is_not_ready(self) -> None:
        adapter = DeepSeekProviderAdapter(FakeTransport())

        self.assertFalse(adapter.provider_ready)
        self.assertFalse(adapter.config.enabled)
        self.assertFalse(adapter.config.authorized)

    def test_enabled_without_authorization_stays_not_ready(self) -> None:
        adapter = make_adapter(authorized=False)

        self.assertFalse(adapter.provider_ready)

    def test_authorized_adapter_reports_ready(self) -> None:
        adapter = make_adapter()

        self.assertTrue(adapter.provider_ready)

    def test_authorization_requires_enabled_and_is_bool(self) -> None:
        with self.assertRaisesRegex(ContractError, "authorized requires enabled"):
            DeepSeekAdapterConfig(enabled=False, authorized=True)
        for value in (1, "true", None):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    DeepSeekAdapterConfig(enabled=True, authorized=value)  # type: ignore[arg-type]


class ProviderIntegrationReadinessTests(unittest.TestCase):
    def test_transport_ready_requires_deepseek_adapter_and_flag(self) -> None:
        integration = ProviderIntegration(
            LocalRouter(),
            config=ProviderIntegrationConfig(provider="deepseek"),
            deepseek_adapter=make_adapter(),
            deepseek_transport_ready=True,
        )

        capabilities = integration.capabilities

        self.assertTrue(capabilities.transport_ready)
        self.assertTrue(capabilities.provider_ready)
        self.assertTrue(capabilities.adapter_ready)
        self.assertFalse(capabilities.runtime_ready)
        self.assertFalse(capabilities.agent_ready)

    def test_local_provider_never_claims_transport_readiness(self) -> None:
        integration = ProviderIntegration(
            LocalRouter(),
            config=ProviderIntegrationConfig(provider="local"),
            deepseek_adapter=make_adapter(),
            deepseek_transport_ready=True,
        )

        capabilities = integration.capabilities

        self.assertFalse(capabilities.transport_ready)
        self.assertEqual(capabilities.provider, "local")
        self.assertTrue(capabilities.provider_ready)

    def test_transport_flag_without_adapter_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "requires an injected adapter"):
            ProviderIntegration(
                LocalRouter(),
                config=ProviderIntegrationConfig(provider="deepseek"),
                deepseek_transport_ready=True,
            )

    def test_router_metadata_matches_selection(self) -> None:
        self.assertEqual(
            ProviderIntegration(LocalRouter()).router_metadata,
            ("local", "chat-only-router", "1"),
        )
        deepseek = ProviderIntegration(
            LocalRouter(),
            config=ProviderIntegrationConfig(provider="deepseek"),
            deepseek_adapter=make_adapter(model_id="deepseek-flash", model_version="v1"),
            deepseek_transport_ready=True,
        )

        self.assertEqual(
            deepseek.router_metadata,
            ("deepseek-official", "deepseek-flash", "v1"),
        )

    def test_missing_adapter_does_not_fall_back_to_local_router(self) -> None:
        local = LocalRouter()
        integration = ProviderIntegration(
            local,
            config=ProviderIntegrationConfig(provider="deepseek"),
        )
        trace = RouteTrace(
            request_id="request-gate",
            conversation_id="conversation-gate",
            turn_id="turn-gate",
            source="text_shell",
            raw_text="你好",
            created_at="2026-09-30T10:00:00Z",
        )

        with self.assertRaisesRegex(ContractError, "requires an injected adapter"):
            integration.route(RouteInput(raw_text="你好", trace=trace))
        self.assertEqual(local.calls, 0)


class MainProviderGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "project"
        self.root.mkdir()
        self.private_root = Path(self.temp_dir.name) / "private-data"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def settings(self, **env: str) -> Settings:
        base = {"ASSISTANT_DATA_ROOT": str(self.private_root)}
        base.update(env)
        return load_settings(project_root=self.root, environ=base)

    def test_default_local_builds_no_transport(self) -> None:
        integration = build_provider_integration(self.settings())

        capabilities = integration.capabilities
        self.assertEqual(capabilities.provider, "local")
        self.assertFalse(capabilities.adapter_injected)
        self.assertFalse(capabilities.transport_ready)
        self.assertTrue(capabilities.provider_ready)

    def test_deepseek_without_opt_in_stays_not_ready_and_offline(self) -> None:
        integration = build_provider_integration(
            self.settings(ASSISTANT_PROVIDER="deepseek")
        )

        capabilities = integration.capabilities
        self.assertEqual(capabilities.provider, "deepseek")
        self.assertFalse(capabilities.adapter_injected)
        self.assertFalse(capabilities.provider_ready)
        self.assertFalse(capabilities.transport_ready)

    def test_deepseek_with_opt_in_wires_real_transport_without_request(self) -> None:
        integration = build_provider_integration(
            self.settings(
                ASSISTANT_PROVIDER="deepseek",
                ASSISTANT_DEEPSEEK_OPT_IN="true",
            )
        )

        capabilities = integration.capabilities
        self.assertTrue(capabilities.adapter_injected)
        self.assertTrue(capabilities.adapter_ready)
        self.assertTrue(capabilities.provider_ready)
        self.assertTrue(capabilities.transport_ready)
        self.assertFalse(capabilities.runtime_ready)
        self.assertFalse(capabilities.agent_ready)

    def test_wired_transport_uses_the_authorized_parameters(self) -> None:
        integration = build_provider_integration(
            self.settings(
                ASSISTANT_PROVIDER="deepseek",
                ASSISTANT_DEEPSEEK_OPT_IN="on",
            )
        )
        adapter = integration._deepseek_adapter  # noqa: SLF001 - 只读断言装配参数

        self.assertIsNotNone(adapter)
        config = adapter.config  # type: ignore[union-attr]
        self.assertEqual(config.provider_name, "deepseek-official")
        self.assertEqual(config.model_id, "deepseek-flash")
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.allowed_hosts, ("api.deepseek.com",))
        self.assertEqual(config.timeout_seconds, 15.0)
        self.assertEqual(config.max_retries, 0)
        self.assertEqual(config.max_tokens, 256)
        self.assertEqual(config.thinking, "disabled")
        self.assertTrue(config.enabled)
        self.assertTrue(config.authorized)

    def test_build_does_not_require_or_read_credential(self) -> None:
        environment = {
            "ASSISTANT_DATA_ROOT": str(self.private_root),
            "ASSISTANT_PROVIDER": "deepseek",
            "ASSISTANT_DEEPSEEK_OPT_IN": "true",
        }

        integration = build_provider_integration(
            load_settings(project_root=self.root, environ=environment)
        )

        self.assertTrue(integration.capabilities.transport_ready)

    def test_invalid_opt_in_value_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.settings(ASSISTANT_DEEPSEEK_OPT_IN="sure")


class ChatAgentWiringTests(unittest.TestCase):
    """批次 B：真实对话 Agent 的双条件装配与就绪投影。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "project"
        self.root.mkdir()
        self.private_root = Path(self.temp_dir.name) / "private-data"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def settings(self, **env: str) -> Settings:
        base = {"ASSISTANT_DATA_ROOT": str(self.private_root)}
        base.update(env)
        return load_settings(project_root=self.root, environ=base)

    def test_default_and_not_opted_in_build_no_agent(self) -> None:
        self.assertIsNone(build_agent_adapter(self.settings()))
        self.assertIsNone(
            build_agent_adapter(self.settings(ASSISTANT_PROVIDER="deepseek"))
        )

    def test_opted_in_builds_ready_agent_without_request_or_credential(self) -> None:
        environment = {
            "ASSISTANT_DATA_ROOT": str(self.private_root),
            "ASSISTANT_PROVIDER": "deepseek",
            "ASSISTANT_DEEPSEEK_OPT_IN": "true",
        }

        agent = build_agent_adapter(
            load_settings(project_root=self.root, environ=environment)
        )

        self.assertIsNotNone(agent)
        self.assertTrue(agent.provider_ready)  # type: ignore[union-attr]
        config = agent.config  # type: ignore[union-attr]
        self.assertEqual(config.provider_name, "deepseek-official")
        self.assertEqual(config.model_id, "deepseek-flash")
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.allowed_hosts, ("api.deepseek.com",))
        self.assertEqual(config.timeout_seconds, 30.0)
        self.assertEqual(config.max_tokens, 512)
        self.assertEqual(config.max_retries, 0)
        self.assertEqual(config.thinking, "disabled")
        self.assertNotIn("response_format", config.system_prompt)
        self.assertEqual(config.model_version, "chat-2026-09-30")

    def test_agent_readiness_is_projected_into_capabilities(self) -> None:
        settings = self.settings(
            ASSISTANT_PROVIDER="deepseek",
            ASSISTANT_DEEPSEEK_OPT_IN="true",
        )

        integration = build_provider_integration(settings, agent_ready=True)
        capabilities = integration.capabilities

        self.assertTrue(capabilities.transport_ready)
        self.assertTrue(capabilities.agent_ready)
        self.assertTrue(capabilities.runtime_ready)

    def test_default_local_never_claims_agent_or_runtime_readiness(self) -> None:
        integration = build_provider_integration(self.settings(), agent_ready=False)

        capabilities = integration.capabilities

        self.assertFalse(capabilities.agent_ready)
        self.assertFalse(capabilities.runtime_ready)

    def test_agent_readiness_requires_transport_readiness(self) -> None:
        with self.assertRaisesRegex(ContractError, "requires transport readiness"):
            ProviderIntegration(
                LocalRouter(),
                config=ProviderIntegrationConfig(provider="deepseek"),
                agent_ready=True,
            )


class RuntimeRouterSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "project"
        self.root.mkdir()
        self.private_root = Path(self.temp_dir.name) / "private-data"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "project"
        self.root.mkdir()
        self.private_root = Path(self.temp_dir.name) / "private-data"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def settings(self, **env: str) -> Settings:
        base = {"ASSISTANT_DATA_ROOT": str(self.private_root)}
        base.update(env)
        return load_settings(project_root=self.root, environ=base)

    def test_default_local_does_not_take_over_routing(self) -> None:
        integration = build_provider_integration(self.settings())

        router, metadata = select_runtime_router(None, integration)

        self.assertIsNone(router)
        self.assertIsNone(metadata)

    def test_explicit_router_always_wins(self) -> None:
        integration = build_provider_integration(
            self.settings(
                ASSISTANT_PROVIDER="deepseek",
                ASSISTANT_DEEPSEEK_OPT_IN="true",
            )
        )
        injected = LocalRouter()

        router, metadata = select_runtime_router(injected, integration)

        self.assertIs(router, injected)
        self.assertIsNone(metadata)

    def test_ready_deepseek_transport_takes_over_with_real_metadata(self) -> None:
        integration = build_provider_integration(
            self.settings(
                ASSISTANT_PROVIDER="deepseek",
                ASSISTANT_DEEPSEEK_OPT_IN="true",
            )
        )

        router, metadata = select_runtime_router(None, integration)

        self.assertIs(router, integration)
        self.assertEqual(
            metadata,
            ("deepseek-official", "deepseek-flash", "runtime-2026-09-30"),
        )

    def test_deepseek_without_opt_in_does_not_take_over(self) -> None:
        integration = build_provider_integration(
            self.settings(ASSISTANT_PROVIDER="deepseek")
        )

        router, metadata = select_runtime_router(None, integration)

        self.assertIsNone(router)
        self.assertIsNone(metadata)


class ProviderStartupNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "project"
        self.root.mkdir()
        self.private_root = Path(self.temp_dir.name) / "private-data"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def settings(self, **env: str) -> Settings:
        base = {"ASSISTANT_DATA_ROOT": str(self.private_root)}
        base.update(env)
        return load_settings(project_root=self.root, environ=base)

    def test_local_provider_has_no_notice(self) -> None:
        settings = self.settings()
        integration = build_provider_integration(settings)

        self.assertIsNone(provider_startup_notice(settings, integration.capabilities))

    def test_deepseek_without_opt_in_explains_fail_closed(self) -> None:
        settings = self.settings(ASSISTANT_PROVIDER="deepseek")
        integration = build_provider_integration(settings)

        notice = provider_startup_notice(settings, integration.capabilities)

        self.assertIsNotNone(notice)
        self.assertIn("ASSISTANT_DEEPSEEK_OPT_IN", notice or "")
        self.assertIn("不会静默回退", notice or "")

    def test_deepseek_with_opt_in_discloses_billing_and_no_fallback(self) -> None:
        settings = self.settings(
            ASSISTANT_PROVIDER="deepseek",
            ASSISTANT_DEEPSEEK_OPT_IN="true",
        )
        integration = build_provider_integration(settings)

        notice = provider_startup_notice(settings, integration.capabilities)

        self.assertIsNotNone(notice)
        self.assertIn("计费", notice or "")
        self.assertIn("api.deepseek.com", notice or "")
        self.assertIn("不回退", notice or "")

    def test_chat_ready_discloses_two_requests_per_turn(self) -> None:
        settings = self.settings(
            ASSISTANT_PROVIDER="deepseek",
            ASSISTANT_DEEPSEEK_OPT_IN="true",
        )
        integration = build_provider_integration(settings, agent_ready=True)

        notice = provider_startup_notice(
            settings, integration.capabilities, chat_ready=True
        )

        self.assertIsNotNone(notice)
        self.assertIn("2 次", notice or "")
        self.assertIn("计费", notice or "")
        self.assertIn("不回退", notice or "")
        self.assertIn("不伪造", notice or "")

    def test_transport_only_notice_still_avoids_claiming_chat(self) -> None:
        settings = self.settings(
            ASSISTANT_PROVIDER="deepseek",
            ASSISTANT_DEEPSEEK_OPT_IN="true",
        )
        integration = build_provider_integration(settings, agent_ready=False)

        notice = provider_startup_notice(settings, integration.capabilities)

        self.assertIsNotNone(notice)
        self.assertNotIn("2 次", notice or "")
        self.assertIn("路由", notice or "")


if __name__ == "__main__":
    unittest.main()
