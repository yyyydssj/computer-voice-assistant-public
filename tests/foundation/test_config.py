from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config.settings import ConfigError, inject_dotenv_secret, load_settings


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "project"
        self.root.mkdir()
        self.private_root = Path(self.temp_dir.name) / "private-data"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_defaults_keep_private_data_outside_repository(self) -> None:
        settings = load_settings(project_root=self.root, environ={})
        self.assertNotEqual(settings.data_root, self.root)
        self.assertNotIn(self.root, settings.data_root.parents)
        self.assertEqual(settings.wake_word, "小饼")
        self.assertFalse(settings.sensitive.configured)

    def test_provider_defaults_to_local_and_summary_is_explicit(self) -> None:
        settings = load_settings(
            project_root=self.root,
            environ={"ASSISTANT_DATA_ROOT": str(self.private_root)},
        )

        self.assertEqual(settings.provider, "local")
        self.assertEqual(settings.public_summary()["provider"], "local")
        self.assertFalse(settings.public_summary()["provider_ready"])

    def test_provider_selection_is_read_without_enabling_runtime(self) -> None:
        settings = load_settings(
            project_root=self.root,
            environ={
                "ASSISTANT_DATA_ROOT": str(self.private_root),
                "ASSISTANT_PROVIDER": "DeepSeek",
                "DEEPSEEK_API_KEY": "must-not-be-exposed",
            },
        )

        self.assertEqual(settings.provider, "deepseek")
        self.assertEqual(settings.public_summary()["provider"], "deepseek")
        self.assertFalse(settings.public_summary()["provider_ready"])
        self.assertNotIn("must-not-be-exposed", repr(settings.public_summary()))

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "ASSISTANT_PROVIDER must be local or deepseek"):
            load_settings(
                project_root=self.root,
                environ={
                    "ASSISTANT_DATA_ROOT": str(self.private_root),
                    "ASSISTANT_PROVIDER": "remote",
                },
            )

    def test_deepseek_opt_in_defaults_to_false(self) -> None:
        settings = load_settings(
            project_root=self.root,
            environ={"ASSISTANT_DATA_ROOT": str(self.private_root)},
        )

        self.assertFalse(settings.deepseek_opt_in)
        self.assertFalse(settings.public_summary()["deepseek_opt_in"])
        self.assertFalse(settings.public_summary()["provider_ready"])

    def test_deepseek_opt_in_accepts_documented_true_tokens(self) -> None:
        for token in ("true", "TRUE", "True", "1", "yes", "on", " true "):
            with self.subTest(token=token):
                settings = load_settings(
                    project_root=self.root,
                    environ={
                        "ASSISTANT_DATA_ROOT": str(self.private_root),
                        "ASSISTANT_DEEPSEEK_OPT_IN": token,
                    },
                )
                self.assertTrue(settings.deepseek_opt_in)
                self.assertTrue(settings.public_summary()["deepseek_opt_in"])

    def test_deepseek_opt_in_accepts_documented_false_tokens(self) -> None:
        for token in ("false", "FALSE", "0", "no", "off"):
            with self.subTest(token=token):
                settings = load_settings(
                    project_root=self.root,
                    environ={
                        "ASSISTANT_DATA_ROOT": str(self.private_root),
                        "ASSISTANT_DEEPSEEK_OPT_IN": token,
                    },
                )
                self.assertFalse(settings.deepseek_opt_in)

    def test_invalid_deepseek_opt_in_is_rejected(self) -> None:
        for token in ("maybe", "2", "enabled", "oui"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ConfigError, "ASSISTANT_DEEPSEEK_OPT_IN"):
                    load_settings(
                        project_root=self.root,
                        environ={
                            "ASSISTANT_DATA_ROOT": str(self.private_root),
                            "ASSISTANT_DEEPSEEK_OPT_IN": token,
                        },
                    )

    def test_deepseek_opt_in_does_not_expose_or_require_secret_value(self) -> None:
        settings = load_settings(
            project_root=self.root,
            environ={
                "ASSISTANT_DATA_ROOT": str(self.private_root),
                "ASSISTANT_PROVIDER": "deepseek",
                "ASSISTANT_DEEPSEEK_OPT_IN": "true",
                "DEEPSEEK_API_KEY": "must-not-be-exposed",
            },
        )

        summary = repr(settings.public_summary())
        self.assertTrue(settings.deepseek_opt_in)
        self.assertNotIn("must-not-be-exposed", summary)

    def test_inject_dotenv_secret_writes_allowlisted_value_into_process_environment(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text("DEEPSEEK_API_KEY=dotenv-secret\n", encoding="utf-8")
        cleaned = {key: value for key, value in os.environ.items() if key != "DEEPSEEK_API_KEY"}

        with patch.dict(os.environ, cleaned, clear=True):
            injected = inject_dotenv_secret("DEEPSEEK_API_KEY", dotenv_path=env_file)

            self.assertTrue(injected)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "dotenv-secret")

    def test_inject_dotenv_secret_never_overrides_existing_process_value(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text("DEEPSEEK_API_KEY=dotenv-secret\n", encoding="utf-8")

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "process-secret"}, clear=False):
            injected = inject_dotenv_secret("DEEPSEEK_API_KEY", dotenv_path=env_file)

            self.assertFalse(injected)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "process-secret")

    def test_inject_dotenv_secret_rejects_non_allowlisted_names(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text("VOLC_TTS_API_KEY=dotenv-secret\n", encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "不允许从 .env 注入"):
            inject_dotenv_secret("VOLC_TTS_API_KEY", dotenv_path=env_file)

    def test_inject_dotenv_secret_is_silent_and_handles_missing_values(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text("WAKE_WORD=小饼\n", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        cleaned = {key: value for key, value in os.environ.items() if key != "DEEPSEEK_API_KEY"}

        with patch.dict(os.environ, cleaned, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                injected_missing = inject_dotenv_secret("DEEPSEEK_API_KEY", dotenv_path=env_file)
                injected_absent_file = inject_dotenv_secret(
                    "DEEPSEEK_API_KEY", dotenv_path=self.root / "missing.env"
                )

            self.assertFalse(injected_missing)
            self.assertFalse(injected_absent_file)
            self.assertNotIn("DEEPSEEK_API_KEY", os.environ)

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_dotenv_is_loaded_and_environment_wins(self) -> None:
        sensitive_key = "VOLC_TTS_API_KEY"
        (self.root / ".env").write_text(
            "WAKE_WORD=文件唤醒\n"
            "WAKE_SENSITIVITY=0.2\n"
            f"{sensitive_key}=dotenv-marker\n",
            encoding="utf-8",
        )
        settings = load_settings(
            project_root=self.root,
            environ={
                "ASSISTANT_DATA_ROOT": str(self.private_root),
                "WAKE_WORD": "环境唤醒",
                "WAKE_SENSITIVITY": "0.8",
                "VOLC_TTS_API_KEY": "environment-marker",
            },
        )
        self.assertEqual(settings.wake_word, "环境唤醒")
        self.assertEqual(settings.wake_sensitivity, 0.8)
        self.assertTrue(settings.sensitive.has("VOLC_TTS_API_KEY"))
        self.assertNotIn("dotenv-marker", repr(settings.public_summary()))
        self.assertNotIn("environment-marker", repr(settings.public_summary()))

    def test_missing_provider_key_is_safe_for_text_mode(self) -> None:
        settings = load_settings(
            project_root=self.root,
            environ={"ASSISTANT_DATA_ROOT": str(self.private_root)},
        )
        self.assertFalse(settings.sensitive.configured)
        self.assertEqual(settings.public_summary()["mode"], "text")
        self.assertFalse(settings.public_summary()["provider_ready"])

    def test_prepare_directories_creates_only_private_runtime_directories(self) -> None:
        settings = load_settings(
            project_root=self.root,
            environ={"ASSISTANT_DATA_ROOT": str(self.private_root)},
        )
        settings.prepare_directories()
        for directory in (
            settings.data_root,
            settings.model_root,
            settings.logs_root,
            settings.plans_root,
            settings.audit_root,
            settings.temp_root,
        ):
            self.assertTrue(directory.is_dir())
        self.assertFalse((self.root / "logs").exists())

    def test_repository_data_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "项目仓库内"):
            load_settings(
                project_root=self.root,
                environ={"ASSISTANT_DATA_ROOT": str(self.root / "data")},
            )

    def test_unc_data_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "UNC"):
            load_settings(
                project_root=self.root,
                environ={"ASSISTANT_DATA_ROOT": r"\\server\share\assistant"},
            )

    def test_invalid_numeric_config_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "必须是数字"):
            load_settings(
                project_root=self.root,
                environ={
                    "ASSISTANT_DATA_ROOT": str(self.private_root),
                    "WAKE_SENSITIVITY": "not-a-number",
                },
            )

    def test_malformed_dotenv_is_rejected(self) -> None:
        (self.root / ".env").write_text("BROKEN_LINE\n", encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "缺少 '='"):
            load_settings(project_root=self.root, environ={})

    def test_prepare_rejects_file_as_runtime_directory(self) -> None:
        self.private_root.write_text("not a directory", encoding="utf-8")
        settings = load_settings(
            project_root=self.root,
            environ={"ASSISTANT_DATA_ROOT": str(self.private_root)},
        )
        with self.assertRaisesRegex(ConfigError, "不是目录"):
            settings.prepare_directories()

    def test_custom_runtime_directories_must_belong_to_data_root(self) -> None:
        runtime_keys = (
            "ASSISTANT_LOGS_ROOT",
            "ASSISTANT_PLANS_ROOT",
            "ASSISTANT_AUDIT_ROOT",
            "ASSISTANT_TEMP_ROOT",
        )
        for key in runtime_keys:
            with self.subTest(key=key):
                with self.assertRaisesRegex(ConfigError, "数据根目录内"):
                    load_settings(
                        project_root=self.root,
                        environ={
                            "ASSISTANT_DATA_ROOT": str(self.private_root),
                            key: str(Path(self.temp_dir.name) / (key.lower() + "-outside")),
                        },
                    )

    def test_custom_runtime_directories_inside_data_root_are_allowed(self) -> None:
        settings = load_settings(
            project_root=self.root,
            environ={
                "ASSISTANT_DATA_ROOT": str(self.private_root),
                "ASSISTANT_LOGS_ROOT": str(self.private_root / "custom-logs"),
                "ASSISTANT_PLANS_ROOT": str(self.private_root / "custom-plans"),
                "ASSISTANT_AUDIT_ROOT": str(self.private_root / "custom-audit"),
                "ASSISTANT_TEMP_ROOT": str(self.private_root / "custom-temp"),
            },
        )
        settings.prepare_directories()
        self.assertEqual(settings.logs_root, self.private_root / "custom-logs")
        self.assertEqual(settings.plans_root, self.private_root / "custom-plans")
        self.assertEqual(settings.audit_root, self.private_root / "custom-audit")
        self.assertEqual(settings.temp_root, self.private_root / "custom-temp")

    def test_runtime_directories_must_not_overlap(self) -> None:
        with self.assertRaisesRegex(ConfigError, "相互隔离"):
            load_settings(
                project_root=self.root,
                environ={
                    "ASSISTANT_DATA_ROOT": str(self.private_root),
                    "ASSISTANT_LOGS_ROOT": str(self.private_root / "shared"),
                    "ASSISTANT_TEMP_ROOT": str(self.private_root / "shared" / "temp"),
                },
            )

    def test_each_runtime_directory_rejects_repository_and_unc_paths(self) -> None:
        runtime_keys = (
            "ASSISTANT_LOGS_ROOT",
            "ASSISTANT_PLANS_ROOT",
            "ASSISTANT_AUDIT_ROOT",
            "ASSISTANT_TEMP_ROOT",
        )
        for key in runtime_keys:
            with self.subTest(key=key, case="repository"):
                with self.assertRaisesRegex(ConfigError, "项目仓库内"):
                    load_settings(
                        project_root=self.root,
                        environ={
                            "ASSISTANT_DATA_ROOT": str(self.private_root),
                            key: str(self.root / key.lower()),
                        },
                    )
            with self.subTest(key=key, case="unc"):
                with self.assertRaisesRegex(ConfigError, "UNC"):
                    load_settings(
                        project_root=self.root,
                        environ={
                            "ASSISTANT_DATA_ROOT": str(self.private_root),
                            key: r"\\server\share\assistant",
                        },
                    )

    def test_each_runtime_directory_rejects_file_instead_of_directory(self) -> None:
        runtime_keys = (
            "ASSISTANT_LOGS_ROOT",
            "ASSISTANT_PLANS_ROOT",
            "ASSISTANT_AUDIT_ROOT",
            "ASSISTANT_TEMP_ROOT",
        )
        for index, key in enumerate(runtime_keys):
            with self.subTest(key=key):
                target = self.private_root / f"file-{index}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("not a directory", encoding="utf-8")
                settings = load_settings(
                    project_root=self.root,
                    environ={
                        "ASSISTANT_DATA_ROOT": str(self.private_root),
                        key: str(target),
                    },
                )
                with self.assertRaisesRegex(ConfigError, "不是目录"):
                    settings.prepare_directories()
                target.unlink()

    def test_prepare_reports_unwritable_runtime_directory(self) -> None:
        settings = load_settings(
            project_root=self.root,
            environ={"ASSISTANT_DATA_ROOT": str(self.private_root)},
        )
        original = tempfile.NamedTemporaryFile

        def fail_for_audit(*args, **kwargs):
            if Path(kwargs["dir"]) == settings.audit_root:
                raise PermissionError("fault injection")
            return original(*args, **kwargs)

        with patch("app.config.settings.tempfile.NamedTemporaryFile", side_effect=fail_for_audit):
            with self.assertRaisesRegex(ConfigError, "不可写"):
                settings.prepare_directories()

    def test_relative_runtime_directory_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "绝对路径"):
            load_settings(
                project_root=self.root,
                environ={
                    "ASSISTANT_DATA_ROOT": str(self.private_root),
                    "ASSISTANT_TEMP_ROOT": "relative-temp",
                },
            )

    def test_process_environment_is_not_leaked_into_dotenv_values(self) -> None:
        with patch.dict(os.environ, {"VOLC_TTS_API_KEY": "process-marker"}, clear=False):
            settings = load_settings(
                project_root=self.root,
                environ={},
            )
        self.assertFalse(settings.sensitive.has("VOLC_TTS_API_KEY"))


class VoiceStudioTransportSettingsTests(unittest.TestCase):
    """P7 Batch 5: dual-path transport settings stay opt-in and loopback-only."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "project"
        self.root.mkdir()
        self.private_root = Path(self.temp_dir.name) / "private-data"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def load(self, **overrides: str):
        environ = {"ASSISTANT_DATA_ROOT": str(self.private_root)}
        environ.update(overrides)
        return load_settings(project_root=self.root, environ=environ)

    def test_defaults_keep_transport_disabled_and_loopback(self) -> None:
        settings = self.load()
        self.assertEqual(settings.tts_voicestudio_mode, "disabled")
        self.assertEqual(settings.tts_ws_url, "ws://127.0.0.1:3900/ws/tts")
        self.assertEqual(settings.tts_http_generate_url, "http://127.0.0.1:3900/generate")
        self.assertEqual(settings.tts_ws_connect_timeout_s, 2.0)
        self.assertEqual(settings.tts_sentence_timeout_s, 18.0)
        self.assertEqual(settings.tts_ws_frame_idle_timeout_s, 3.0)
        self.assertEqual(settings.tts_ws_session_idle_s, 30.0)
        self.assertEqual(settings.tts_pcm_start_buffer_ms, 160)
        self.assertEqual(settings.tts_pcm_max_queued_audio_ms, 8000)
        self.assertFalse(settings.public_summary()["provider_ready"])

    def test_ws_pcm_mode_is_accepted(self) -> None:
        settings = self.load(TTS_VOICESTUDIO_MODE="WS_PCM")
        self.assertEqual(settings.tts_voicestudio_mode, "ws_pcm")
        self.assertEqual(settings.public_summary()["tts_voicestudio_mode"], "ws_pcm")

    def test_unknown_mode_is_rejected_with_the_full_allowed_list(self) -> None:
        with self.assertRaisesRegex(
            ConfigError, "TTS_VOICESTUDIO_MODE must be disabled, http_wav, ws_pcm or auto"
        ):
            self.load(TTS_VOICESTUDIO_MODE="streaming")

    def test_non_loopback_transport_urls_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "loopback"):
            self.load(TTS_WS_URL="ws://tts.example.com:3900/ws/tts")
        with self.assertRaisesRegex(ConfigError, "loopback"):
            self.load(TTS_HTTP_GENERATE_URL="http://192.168.1.10:3900/generate")

    def test_credentials_query_and_wrong_scheme_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "凭据"):
            self.load(TTS_WS_URL="ws://user:pass@127.0.0.1:3900/ws/tts")
        with self.assertRaisesRegex(ConfigError, "凭据"):
            self.load(TTS_WS_URL="ws://127.0.0.1:3900/ws/tts?token=1")
        with self.assertRaisesRegex(ConfigError, "必须是 http/https"):
            self.load(TTS_HTTP_GENERATE_URL="ws://127.0.0.1:3900/generate")

    def test_timeout_and_buffer_ranges_are_enforced(self) -> None:
        with self.assertRaisesRegex(ConfigError, "TTS_SENTENCE_TIMEOUT_S"):
            self.load(TTS_SENTENCE_TIMEOUT_S="0")
        with self.assertRaisesRegex(ConfigError, "TTS_WS_CONNECT_TIMEOUT_S"):
            self.load(TTS_WS_CONNECT_TIMEOUT_S="9999")
        with self.assertRaisesRegex(ConfigError, "TTS_PCM_START_BUFFER_MS"):
            self.load(TTS_PCM_START_BUFFER_MS="0")
        with self.assertRaisesRegex(ConfigError, "TTS_PCM_MAX_QUEUED_AUDIO_MS"):
            self.load(TTS_PCM_MAX_QUEUED_AUDIO_MS="-5")

    def test_explicit_values_are_read_without_enabling_runtime(self) -> None:
        settings = self.load(
            TTS_VOICESTUDIO_MODE="auto",
            TTS_WS_CONNECT_TIMEOUT_S="1.5",
            TTS_PCM_START_BUFFER_MS="240",
            TTS_PCM_MAX_QUEUED_AUDIO_MS="4000",
        )
        self.assertEqual(settings.tts_voicestudio_mode, "auto")
        self.assertEqual(settings.tts_ws_connect_timeout_s, 1.5)
        self.assertEqual(settings.tts_pcm_start_buffer_ms, 240)
        self.assertEqual(settings.tts_pcm_max_queued_audio_ms, 4000)
        summary = settings.public_summary()
        self.assertTrue(summary["tts_ws_url_loopback"])
        self.assertEqual(summary["tts_pcm_start_buffer_ms"], 240)
        self.assertNotIn("127.0.0.1:3900", repr(summary))


if __name__ == "__main__":
    unittest.main()
