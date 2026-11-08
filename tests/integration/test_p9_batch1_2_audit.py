"""P9 批次 1-2 只读审计测试（task-3）。

本文件是审计者的独立复现证据，不修改任何源码，也不替代写入者自己的专项测试：

1. UTF-8 严格性：被审源码与测试文件无 BOM、无 ``U+FFFD``、可按 UTF-8 严格解码。
2. 执行面隔离：被审源码无进程/网络/线程/设备导入，无 ``eval``/``exec``/``open`` 等危险调用；
   对 ``app.safety`` / ``app.executor`` / ``app.plans`` / ``app.auditing`` / ``app.verification`` /
   ``app.tools`` 的引用数为 0。
3. 冻结接口：逐条核对规格（``2026-09-30-P9-batch1-2-interface-freeze.md`` §2/§3/§4/§5）的
   类名、枚举成员与取值、dataclass 字段顺序、方法签名与默认值。
4. 关键语义：规格 §2.1/§2.2/§3/§4/§5 硬约束的独立行为断言（含对抗性输入）。

审计基准：全量 ``unittest discover -s tests`` 基线 ``Ran 1394 tests ... OK (skipped=3)``；
本批次新增后（audit 首版快照）为 ``Ran 1502 tests ... OK (skipped=3)``。

Lead 裁定（2026-10-01，审计第二版据此更新）：
- ``AutoCaptureEndReason`` 末尾纯增量追加 ``SESSION_CLOSED`` / ``SESSION_ENDED_INTERNAL`` /
  ``SESSION_INVALIDATED``（共 17 项），原 14 项名称与取值一字不动 —— 本文件断言前 14 项不变。
- ``AutoCaptureDecision.passive_decision`` 第 7 字段保留为诊断专用，不进入授权/交付判定 ——
  本文件断言其为可缺省字段，并验证规格形状的 6 字段构造仍可用。
- ``cancel()`` 语义放宽为：幂等，且只要 bus 仍 ``OPEN`` 就关闭（``discard=True``）；终止态
  （ENDED/FAILED）下不迁移状态、不改写 ``terminal_reason``，但作废未交付帧（``deliver()`` 返回空）。
"""

from __future__ import annotations

import ast
import inspect
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.audio.bus import AudioBus, AudioBusState, AudioPacket
from app.audio.vad import VadConfig
from app.audio.vad.auto_capture import (
    AutoCaptureConfig,
    AutoCaptureDecision,
    AutoCaptureEndReason,
    AutoCaptureSession,
    AutoCaptureState,
    PassiveFrameDecision,
    PassiveFrameObservation,
    SpeechStartConfig,
    SpeechStartDecision,
    SpeechStartDetector,
)
from app.contracts import AudioFrame
from app.dialog import (
    DIALOG_SCHEMA_VERSION,
    DIALOG_TRANSITION_REASONS,
    DialogCoordinator,
    DialogDiagnostic,
    DialogError,
    DialogInputMode,
    DialogState,
    DialogStateMachine,
    DialogTransition,
    DialogWindow,
    DialogWindowCloseReason,
    DialogWindowConfig,
    DialogWindowState,
    DialogWindowStatus,
    InputPermit,
    TurnClaim,
    TurnClaimDecision,
)
from app.voice_loop.boundary import VoiceInputSource

ROOT = Path(__file__).resolve().parents[2]
DIALOG_DIR = ROOT / "app" / "dialog"
AUTO_CAPTURE_PATH = ROOT / "app" / "audio" / "vad" / "auto_capture.py"

AUDITED_SOURCE_FILES: tuple[Path, ...] = (
    DIALOG_DIR / "__init__.py",
    DIALOG_DIR / "coordinator.py",
    DIALOG_DIR / "state.py",
    DIALOG_DIR / "window.py",
    AUTO_CAPTURE_PATH,
)
AUDITED_TEST_FILES: tuple[Path, ...] = (
    ROOT / "tests" / "dialog" / "__init__.py",
    ROOT / "tests" / "dialog" / "test_coordinator.py",
    ROOT / "tests" / "dialog" / "test_state.py",
    ROOT / "tests" / "dialog" / "test_window.py",
    ROOT / "tests" / "audio" / "test_auto_capture.py",
    ROOT / "tests" / "audio" / "test_vad.py",
    ROOT / "tests" / "integration" / "test_p9_batch1_2_audit.py",
)

SAFETY_MODULE_PREFIXES = (
    "app.safety",
    "app.executor",
    "app.plans",
    "app.auditing",
    "app.verification",
    "app.tools",
)
FORBIDDEN_IMPORT_ROOTS = (
    "subprocess",
    "socket",
    "threading",
    "multiprocessing",
    "ctypes",
    "sounddevice",
    "sherpa_onnx",
    "winmm",
    "pyaudio",
    "soundcard",
    "winsound",
)
DANGEROUS_CALL_NAMES = (
    "open",
    "eval",
    "exec",
    "compile",
    "__import__",
    "os.system",
    "os.popen",
    "os.execv",
    "os.fork",
)

POS = "positional_or_keyword"
KW = "keyword_only"


def _source_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source_text(path), filename=str(path))


def _import_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                names.add(module)
    return names


def _dotted_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts: list[str] = []
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            names.add(".".join(reversed(parts)))
    return names


def _call_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            names.add(f"{func.value.id}.{func.attr}")
    return names


def _signature_names(func: object) -> tuple[tuple[str, str], ...]:
    signature = inspect.signature(func)  # type: ignore[arg-type]
    return tuple(
        (name, parameter.kind.name.lower())
        for name, parameter in signature.parameters.items()
        if name != "self"
    )


class _Clock:
    def __init__(self) -> None:
        self._now = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class P9AuditSourceSetTests(unittest.TestCase):
    """被审文件集合必须与规格 §1 的新增清单一致。"""

    def test_dialog_package_contains_exactly_the_frozen_four_modules(self) -> None:
        actual = {path.name for path in DIALOG_DIR.glob("*.py")}
        self.assertEqual(actual, {"__init__.py", "state.py", "window.py", "coordinator.py"})

    def test_audited_sources_and_tests_exist(self) -> None:
        for path in AUDITED_SOURCE_FILES + AUDITED_TEST_FILES:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), f"缺少被审文件：{path}")


class P9AuditEncodingTests(unittest.TestCase):
    """严格 UTF-8：无 BOM、无替换字符、可严格解码。"""

    def _assert_strict_utf8(self, path: Path) -> None:
        raw = path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{path.name} 含 UTF-8 BOM")
        self.assertNotIn(b"\xef\xbf\xbd", raw, f"{path.name} 含 U+FFFD 替换字符")
        decoded = raw.decode("utf-8")
        self.assertNotIn("\ufffd", decoded, f"{path.name} 解码后含 U+FFFD")

    def test_audited_sources_are_strict_utf8(self) -> None:
        for path in AUDITED_SOURCE_FILES:
            with self.subTest(path=path.name):
                self._assert_strict_utf8(path)

    def test_audited_tests_are_strict_utf8(self) -> None:
        for path in AUDITED_TEST_FILES:
            with self.subTest(path=path.name):
                self._assert_strict_utf8(path)


class P9AuditExecutionSurfaceTests(unittest.TestCase):
    """执行面隔离：无进程/网络/线程/设备入口，无危险调用，安全链引用数为 0。"""

    def test_no_forbidden_import_roots(self) -> None:
        for path in AUDITED_SOURCE_FILES:
            tree = _tree(path)
            found = sorted(
                name
                for name in _import_names(tree)
                if name.split(".")[0] in FORBIDDEN_IMPORT_ROOTS
            )
            with self.subTest(path=path.name):
                self.assertEqual(found, [], f"{path.name} 出现禁用导入：{found}")

    def test_no_dangerous_calls(self) -> None:
        for path in AUDITED_SOURCE_FILES:
            tree = _tree(path)
            found = sorted(_call_names(tree).intersection(DANGEROUS_CALL_NAMES))
            with self.subTest(path=path.name):
                self.assertEqual(found, [], f"{path.name} 出现危险调用：{found}")

    def test_safety_chain_reference_count_is_zero(self) -> None:
        total = 0
        for path in AUDITED_SOURCE_FILES:
            tree = _tree(path)
            candidates = _import_names(tree) | _dotted_names(tree)
            refs = sorted(
                name
                for name in candidates
                if any(
                    name == prefix or name.startswith(f"{prefix}.")
                    for prefix in SAFETY_MODULE_PREFIXES
                )
            )
            total += len(refs)
            with self.subTest(path=path.name):
                self.assertEqual(refs, [], f"{path.name} 引用了安全链模块：{refs}")
        self.assertEqual(total, 0, "安全链引用总数必须为 0")

    def test_auto_capture_has_no_device_or_thread_entrypoint(self) -> None:
        imports = _import_names(_tree(AUTO_CAPTURE_PATH))
        for forbidden in ("sounddevice", "sherpa_onnx", "winmm", "threading", "socket", "subprocess"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imports)

    def test_auto_capture_only_reuses_existing_audio_and_contract_modules(self) -> None:
        imports = _import_names(_tree(AUTO_CAPTURE_PATH))
        app_modules = sorted(name for name in imports if name.startswith("app"))
        self.assertEqual(
            app_modules,
            ["app.audio.bus", "app.contracts", "app.contracts.base"],
            "auto_capture.py 的 app.* 导入面超出复用清单",
        )

    def test_dialog_package_only_reuses_contracts_and_voice_boundary(self) -> None:
        app_modules: set[str] = set()
        for path in (DIALOG_DIR / "state.py", DIALOG_DIR / "window.py", DIALOG_DIR / "coordinator.py"):
            app_modules.update(
                name for name in _import_names(_tree(path)) if name.startswith("app")
            )
        self.assertEqual(
            sorted(app_modules),
            ["app.contracts.base", "app.dialog.state", "app.dialog.window", "app.voice_loop.boundary"],
        )


class P9AuditFrozenStateTests(unittest.TestCase):
    """规格 §2：状态、模式、诊断与迁移记录。"""

    def test_schema_version_and_reason_whitelist(self) -> None:
        self.assertEqual(DIALOG_SCHEMA_VERSION, 1)
        self.assertIsInstance(DIALOG_TRANSITION_REASONS, frozenset)
        required = {
            "turn_started",
            "voice_turn_started",
            "text_turn_started",
            "listening_started",
            "speech_started",
            "speech_finished",
            "transcription_ready",
            "route_handled",
            "clarify_required",
            "reply_ready",
            "authorization_required",
            "execution_requested",
            "speech_started_output",
            "speech_finished_output",
            "turn_completed",
            "idle_timeout",
            "window_timeout",
            "cancelled",
            "stopped",
            "input_degraded",
            "audio_failed",
            "asr_failed",
            "agent_failed",
            "provider_unavailable",
            "user_requested_text",
            "recovered",
            "shutdown",
        }
        self.assertTrue(required.issubset(DIALOG_TRANSITION_REASONS))

    def test_enum_members_and_values(self) -> None:
        self.assertEqual(
            {member.name: member.value for member in DialogState},
            {
                "IDLE": "idle",
                "LISTENING": "listening",
                "USER_SPEAKING": "user_speaking",
                "TRANSCRIBING": "transcribing",
                "THINKING": "thinking",
                "CLARIFYING": "clarifying",
                "AWAITING_AUTHORIZATION": "awaiting_authorization",
                "EXECUTING": "executing",
                "SPEAKING": "speaking",
                "FAIL_CLOSED": "fail_closed",
                "CLOSED": "closed",
            },
        )
        self.assertEqual(
            {member.name: member.value for member in DialogInputMode},
            {"PASSIVE": "passive", "VOICE_TURN": "voice_turn", "TEXT": "text"},
        )

    def test_dataclass_fields_are_frozen_slotted_and_ordered(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(DialogDiagnostic)),
            ("event_type", "subject_id", "reason", "state", "mode"),
        )
        self.assertEqual(
            tuple(field.name for field in fields(DialogTransition)),
            ("subject_id", "from_state", "to_state", "reason", "mode"),
        )
        for cls in (DialogDiagnostic, DialogTransition):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(cls.__dataclass_params__.frozen)
                self.assertIn("__slots__", vars(cls))
        self.assertTrue(callable(DialogDiagnostic.to_details))

    def test_error_and_diagnostic_details(self) -> None:
        self.assertTrue(issubclass(DialogError, RuntimeError))
        diagnostic = DialogDiagnostic(
            event_type="dialog_transition",
            subject_id="dialog-window-1",
            reason="listening_started",
            state=DialogState.LISTENING,
            mode=None,
        )
        details = diagnostic.to_details()
        self.assertEqual(set(details), {"reason", "state", "mode"})
        self.assertTrue(all(isinstance(value, str) for value in details.values()))

    def test_state_machine_signatures(self) -> None:
        expected = {
            "__init__": (("source_instance_id", KW), ("window_id", KW), ("diagnostic_sink", KW)),
            "transition": (("target", POS), ("reason", KW)),
            "begin_voice_turn": (("reason", KW),),
            "begin_text_turn": (("reason", KW),),
            "fail_closed": (("reason", KW),),
            "recover": (("reason", KW),),
            "close": (("reason", KW),),
        }
        for name, signature in expected.items():
            with self.subTest(method=name):
                self.assertEqual(_signature_names(getattr(DialogStateMachine, name)), signature)
        for name in ("state", "mode", "window_id", "source_instance_id", "transitions", "is_terminal", "accepts_input"):
            with self.subTest(prop=name):
                self.assertIsInstance(getattr(DialogStateMachine, name), property)

    def test_default_reason_arguments_match_the_freeze(self) -> None:
        voice = inspect.signature(DialogStateMachine.begin_voice_turn).parameters["reason"]
        text = inspect.signature(DialogStateMachine.begin_text_turn).parameters["reason"]
        self.assertEqual(voice.default, "voice_turn_started")
        self.assertEqual(text.default, "text_turn_started")


class P9AuditFrozenWindowTests(unittest.TestCase):
    """规格 §3：连续对话窗口。"""

    def test_config_defaults_and_validation(self) -> None:
        defaults = {field.name: field.default for field in fields(DialogWindowConfig)}
        self.assertEqual(
            defaults,
            {
                "idle_timeout_seconds": 300,
                "max_turns": 8,
                "max_window_seconds": 1800,
                "listen_timeout_seconds": 8,
            },
        )
        self.assertTrue(DialogWindowConfig.__dataclass_params__.frozen)
        self.assertIn("__slots__", vars(DialogWindowConfig))

    def test_window_enums(self) -> None:
        self.assertEqual(
            {member.name: member.value for member in DialogWindowState},
            {"OPEN": "open", "CLOSED": "closed"},
        )
        self.assertEqual(
            {member.name: member.value for member in DialogWindowCloseReason},
            {
                "IDLE_TIMEOUT": "idle_timeout",
                "MAX_TURNS": "max_turns",
                "MAX_WINDOW_DURATION": "max_window_duration",
                "USER_CANCELLED": "user_cancelled",
                "FAIL_CLOSED": "fail_closed",
                "SHUTDOWN": "shutdown",
            },
        )

    def test_status_fields_and_signatures(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(DialogWindowStatus)),
            ("window_id", "state", "turn_count", "close_reason", "expired"),
        )
        expected = {
            "__init__": (("config", KW), ("now", KW), ("window_id", KW), ("diagnostic_sink", KW)),
            "open": (),
            "note_activity": (),
            "begin_turn": (),
            "end_turn": (),
            "check_timeouts": (),
            "close": (("reason", KW),),
            "is_open": (),
        }
        for name, signature in expected.items():
            with self.subTest(method=name):
                self.assertEqual(_signature_names(getattr(DialogWindow, name)), signature)
        for name in ("window_id", "status", "state", "close_reason"):
            with self.subTest(prop=name):
                self.assertIsInstance(getattr(DialogWindow, name), property)


class P9AuditFrozenCoordinatorTests(unittest.TestCase):
    """规格 §4：turn claim 与输入许可。"""

    def test_dataclass_fields(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(TurnClaim)),
            ("claim_id", "window_id", "turn_id", "source", "layer", "issued_at"),
        )
        self.assertEqual(
            tuple(field.name for field in fields(TurnClaimDecision)),
            ("granted", "claim", "reason"),
        )
        self.assertEqual(
            tuple(field.name for field in fields(InputPermit)),
            ("granted", "reason", "state", "mode"),
        )
        for cls in (TurnClaim, TurnClaimDecision, InputPermit):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(cls.__dataclass_params__.frozen)
                self.assertIn("__slots__", vars(cls))
        self.assertTrue(callable(getattr(TurnClaim, "__post_init__", None)))

    def test_coordinator_signatures(self) -> None:
        expected = {
            "__init__": (("machine", KW), ("window", KW), ("diagnostic_sink", KW)),
            "request_input": (("layer", KW), ("source", KW)),
            "claim_turn": (("layer", KW), ("source", KW), ("turn_id", KW)),
            "release_turn": (("claim", POS), ("reason", KW)),
            "suspend_for_authorization": (("reason", KW),),
            "suspend_for_execution": (("reason", KW),),
            "resume_listening": (("reason", KW),),
            "note_speech_started": (("claim", KW),),
            "note_speech_finished": (("claim", KW),),
            "invalidate": (("reason", KW),),
        }
        for name, signature in expected.items():
            with self.subTest(method=name):
                self.assertEqual(_signature_names(getattr(DialogCoordinator, name)), signature)
        for name in ("machine", "window", "active_claim", "closed_claims"):
            with self.subTest(prop=name):
                self.assertIsInstance(getattr(DialogCoordinator, name), property)


class P9AuditFrozenAutoCaptureTests(unittest.TestCase):
    """规格 §5：自动收音 VAD。"""

    def test_speech_start_config_defaults_and_fields(self) -> None:
        defaults = {field.name: field.default for field in fields(SpeechStartConfig)}
        self.assertEqual(
            defaults,
            {"speech_frame_threshold": 3, "reset_on_miss": True, "min_rms_dbfs": None},
        )
        self.assertEqual(
            tuple(field.name for field in fields(SpeechStartDecision)),
            ("observed", "started", "speech_frames", "observed_frames"),
        )
        self.assertEqual(
            _signature_names(SpeechStartDetector.__init__),
            (("config", KW),),
        )
        self.assertEqual(_signature_names(SpeechStartDetector.observe), (("packet", POS),))
        self.assertEqual(_signature_names(SpeechStartDetector.reset), ())
        for name in ("config", "started", "speech_frames", "observed_frames"):
            with self.subTest(prop=name):
                self.assertIsInstance(getattr(SpeechStartDetector, name), property)

    def test_passive_enums_and_observation_fields(self) -> None:
        self.assertEqual(
            {member.name: member.value for member in PassiveFrameDecision},
            {
                "BUFFERED": "buffered",
                "STALE_AUDIO_SESSION": "stale_audio_session",
                "STALE_GENERATION": "stale_generation",
                "STALE_STREAM": "stale_stream",
                "STALE_DEVICE_EPOCH": "stale_device_epoch",
                "INVALID_AUDIO_FRAME": "invalid_audio_frame",
                "FRAME_DISCONTINUITY": "frame_discontinuity",
                "OVERFLOW": "overflow",
                "INVALIDATED": "invalidated",
            },
        )
        self.assertEqual(
            tuple(field.name for field in fields(PassiveFrameObservation)),
            ("decision", "buffered_frames", "speech_started"),
        )

    def test_auto_capture_config_signature_and_defaults(self) -> None:
        self.assertEqual(
            _signature_names(AutoCaptureConfig.__init__),
            (
                ("vad", KW),
                ("passive_capacity_frames", KW),
                ("speech_start", KW),
                ("max_utterance_ms", KW),
                ("speech_start_timeout_ms", KW),
            ),
        )
        parameters = inspect.signature(AutoCaptureConfig.__init__).parameters
        self.assertEqual(parameters["passive_capacity_frames"].default, 25)
        self.assertIsNone(parameters["speech_start"].default)
        self.assertEqual(parameters["max_utterance_ms"].default, 30000)
        self.assertEqual(parameters["speech_start_timeout_ms"].default, 8000)
        for name in ("vad", "passive_capacity_frames", "speech_start", "max_utterance_ms", "speech_start_timeout_ms"):
            with self.subTest(prop=name):
                self.assertIsInstance(getattr(AutoCaptureConfig, name), property)

    def test_auto_capture_enums(self) -> None:
        original_fourteen = {
            "SILENCE_TIMEOUT": "silence_timeout",
            "MAX_UTTERANCE_DURATION": "max_utterance_duration",
            "SPEECH_START_TIMEOUT": "speech_start_timeout",
            "CANCELLED": "cancelled",
            "INVALID_AUDIO_FRAME": "invalid_audio_frame",
            "FRAME_DISCONTINUITY": "frame_discontinuity",
            "STALE_AUDIO_SESSION": "stale_audio_session",
            "STALE_GENERATION": "stale_generation",
            "STALE_STREAM": "stale_stream",
            "STALE_DEVICE_EPOCH": "stale_device_epoch",
            "DETECTOR_UNAVAILABLE": "detector_unavailable",
            "DETECTOR_FAILURE": "detector_failure",
            "BUS_CLOSED": "bus_closed",
            "INVALIDATED": "invalidated",
        }
        members = list(AutoCaptureEndReason)
        # Lead 裁定：末尾纯增量追加 3 个拒绝原因码，原 14 项名称与取值一字不动。
        self.assertEqual(
            {member.name: member.value for member in members[:14]},
            original_fourteen,
            "原有 14 个 AutoCaptureEndReason 成员被改动，违反纯增量修正",
        )
        self.assertEqual(
            {member.name: member.value for member in members},
            {
                **original_fourteen,
                "SESSION_CLOSED": "session_closed",
                "SESSION_ENDED_INTERNAL": "session_ended",
                "SESSION_INVALIDATED": "session_invalidated",
            },
        )
        self.assertEqual(len(members), 17)
        self.assertEqual(
            {member.name: member.value for member in AutoCaptureState},
            {
                "IDLE": "idle",
                "LISTENING": "listening",
                "CAPTURING": "capturing",
                "ENDED": "ended",
                "FAILED": "failed",
            },
        )

    def test_decision_spec_fields_are_a_leading_frozen_prefix(self) -> None:
        spec_fields = (
            "state",
            "reason",
            "speech_started",
            "buffered_frames",
            "captured_frames",
            "emitted",
        )
        actual = tuple(field.name for field in fields(AutoCaptureDecision))
        self.assertEqual(actual[: len(spec_fields)], spec_fields)
        # Lead 裁定：第 7 字段 passive_decision 保留（诊断专用，不参与授权/交付判定）。
        self.assertEqual(actual[6:], ("passive_decision",))
        self.assertIsNone(
            fields(AutoCaptureDecision)[6].default,
            "passive_decision 必须是可缺省字段，规格形状的 6 字段构造仍须可用",
        )
        spec_shaped = AutoCaptureDecision(
            state=AutoCaptureState.IDLE,
            reason=None,
            speech_started=False,
            buffered_frames=0,
            captured_frames=0,
            emitted=(),
        )
        self.assertIsNone(spec_shaped.passive_decision)
        self.assertTrue(AutoCaptureDecision.__dataclass_params__.frozen)
        self.assertIn("__slots__", vars(AutoCaptureDecision))
        self.assertTrue(callable(AutoCaptureDecision.to_details))

    def test_session_signatures_and_properties(self) -> None:
        expected = {
            "__init__": (("bus", KW), ("config", KW), ("detector", KW), ("start_detector", KW)),
            "listen": (),
            "observe": (("packet", POS),),
            "finish": (),
            "deliver": (),
            "cancel": (),
            "invalidate": (("reason", KW),),
        }
        for name, signature in expected.items():
            with self.subTest(method=name):
                self.assertEqual(_signature_names(getattr(AutoCaptureSession, name)), signature)
        for name in ("state", "terminal_reason", "buffered_frames", "captured_frames", "speech_started"):
            with self.subTest(prop=name):
                self.assertIsInstance(getattr(AutoCaptureSession, name), property)


class _AudioFixture:
    """构造 bus / 帧 / 会话的只读测试夹具。"""

    @staticmethod
    def bus(*, device_epoch: int = 1) -> AudioBus:
        return AudioBus(
            audio_session_id="audio-1",
            provider_generation=1,
            stream_id="stream-1",
            device_epoch=device_epoch,
            capacity_frames=64,
        )

    @staticmethod
    def frame(sequence: int, **overrides: object) -> AudioFrame:
        values: dict[str, object] = {
            "audio_session_id": "audio-1",
            "provider_generation": 1,
            "stream_id": "stream-1",
            "event_sequence": sequence,
            "frame_index": sequence - 1,
            "captured_at": "2026-09-21T08:00:00Z",
            "duration_ms": 20,
            "device_epoch": 1,
            "sample_rate_hz": 16000,
            "channel_count": 1,
            "sample_format": "pcm_s16le",
            "sample_count": 320,
            "payload_size_bytes": 640,
        }
        values.update(overrides)
        return AudioFrame(**values)

    @classmethod
    def speech(cls, sequence: int, **overrides: object) -> AudioPacket:
        return AudioPacket(cls.frame(sequence, **overrides), bytes([1]) + bytes(639))

    @classmethod
    def quiet(cls, sequence: int, **overrides: object) -> AudioPacket:
        return AudioPacket(cls.frame(sequence, **overrides), bytes(640))

    @classmethod
    def loud(cls, sequence: int, **overrides: object) -> AudioPacket:
        return AudioPacket(
            cls.frame(sequence, **overrides),
            b"".join(int(10000).to_bytes(2, "little", signed=True) for _ in range(320)),
        )


class P9AuditDialogSemanticsTests(unittest.TestCase):
    """规格 §2.1/§2.2/§3/§4 硬约束的独立行为断言。"""

    @staticmethod
    def machine() -> DialogStateMachine:
        return DialogStateMachine(source_instance_id="source-1")

    @staticmethod
    def open_window(*, now: _Clock | None = None, **overrides: object) -> DialogWindow:
        window = DialogWindow(config=DialogWindowConfig(**overrides), now=now)
        window.open()
        return window

    def test_accepts_input_matrix_follows_the_frozen_definition(self) -> None:
        cases: list[tuple[str, DialogState, bool]] = []
        base = self.machine()
        cases.append(("idle", base.state, True))

        listening = self.machine()
        listening.transition(DialogState.LISTENING, reason="listening_started")
        cases.append(("listening", listening.state, True))

        speaking = self.machine()
        speaking.begin_voice_turn()
        cases.append(("user_speaking", speaking.state, False))

        transcribing = self.machine()
        transcribing.begin_voice_turn()
        transcribing.transition(DialogState.TRANSCRIBING, reason="speech_finished")
        cases.append(("transcribing", transcribing.state, False))

        thinking = self.machine()
        thinking.begin_text_turn()
        cases.append(("thinking", thinking.state, False))

        clarifying = self.machine()
        clarifying.begin_text_turn()
        clarifying.transition(DialogState.CLARIFYING, reason="clarify_required")
        cases.append(("clarifying", clarifying.state, False))

        awaiting = self.machine()
        awaiting.begin_text_turn()
        awaiting.transition(DialogState.AWAITING_AUTHORIZATION, reason="authorization_required")
        cases.append(("awaiting_authorization", awaiting.state, False))

        executing = self.machine()
        executing.begin_text_turn()
        executing.transition(DialogState.EXECUTING, reason="execution_requested")
        cases.append(("executing", executing.state, False))

        replying = self.machine()
        replying.begin_text_turn()
        replying.transition(DialogState.SPEAKING, reason="reply_ready")
        cases.append(("speaking", replying.state, False))

        failed = self.machine()
        failed.fail_closed(reason="audio_failed")
        cases.append(("fail_closed", failed.state, False))

        closed = self.machine()
        closed.close(reason="shutdown")
        cases.append(("closed", closed.state, False))

        for label, state, expected in cases:
            with self.subTest(state=label):
                machine = self.machine()
                if state is not DialogState.IDLE:
                    self._drive(machine, state)
                self.assertIs(machine.state, state)
                self.assertEqual(machine.accepts_input, expected)
                self.assertEqual(machine.is_terminal, state is DialogState.CLOSED)

    @staticmethod
    def _drive(machine: DialogStateMachine, target: DialogState) -> None:
        path = {
            DialogState.IDLE: (),
            DialogState.LISTENING: ((DialogState.LISTENING, "listening_started"),),
            DialogState.USER_SPEAKING: ((DialogState.USER_SPEAKING, "voice_turn_started"),),
            DialogState.TRANSCRIBING: (
                (DialogState.USER_SPEAKING, "voice_turn_started"),
                (DialogState.TRANSCRIBING, "speech_finished"),
            ),
            DialogState.THINKING: ((DialogState.THINKING, "text_turn_started"),),
            DialogState.CLARIFYING: (
                (DialogState.THINKING, "text_turn_started"),
                (DialogState.CLARIFYING, "clarify_required"),
            ),
            DialogState.AWAITING_AUTHORIZATION: (
                (DialogState.THINKING, "text_turn_started"),
                (DialogState.AWAITING_AUTHORIZATION, "authorization_required"),
            ),
            DialogState.EXECUTING: (
                (DialogState.THINKING, "text_turn_started"),
                (DialogState.EXECUTING, "execution_requested"),
            ),
            DialogState.SPEAKING: (
                (DialogState.THINKING, "text_turn_started"),
                (DialogState.SPEAKING, "reply_ready"),
            ),
            DialogState.FAIL_CLOSED: ((DialogState.FAIL_CLOSED, "audio_failed"),),
            DialogState.CLOSED: ((DialogState.CLOSED, "shutdown"),),
        }[target]
        for state, reason in path:
            machine.transition(state, reason=reason)

    def test_guard_states_only_reachable_from_thinking(self) -> None:
        for start in (DialogState.IDLE, DialogState.LISTENING):
            for guard in (DialogState.AWAITING_AUTHORIZATION, DialogState.EXECUTING):
                machine = self.machine()
                if start is DialogState.LISTENING:
                    machine.transition(DialogState.LISTENING, reason="listening_started")
                with self.subTest(start=start.value, target=guard.value):
                    with self.assertRaises(DialogError):
                        machine.transition(guard, reason="authorization_required")
                    self.assertIs(machine.state, start)
        thinking = self.machine()
        thinking.begin_text_turn()
        thinking.transition(DialogState.AWAITING_AUTHORIZATION, reason="authorization_required")

    def test_closed_is_absorbing(self) -> None:
        machine = self.machine()
        machine.close(reason="shutdown")
        for call in (
            lambda: machine.transition(DialogState.IDLE, reason="turn_completed"),
            lambda: machine.fail_closed(reason="audio_failed"),
            lambda: machine.close(reason="shutdown"),
            lambda: machine.recover(reason="recovered"),
            lambda: machine.begin_voice_turn(),
            lambda: machine.begin_text_turn(),
        ):
            with self.assertRaises(DialogError):
                call()
        self.assertIs(machine.state, DialogState.CLOSED)

    def test_illegal_self_and_free_text_reasons_are_refused(self) -> None:
        machine = self.machine()
        machine.transition(DialogState.LISTENING, reason="listening_started")
        with self.assertRaises(DialogError):
            machine.transition(DialogState.LISTENING, reason="listening_started")
        with self.assertRaises(TypeError):
            machine.transition("idle", reason="turn_completed")  # type: ignore[arg-type]
        with self.assertRaises(DialogError):
            machine.transition(DialogState.IDLE, reason="please stop now")
        with self.assertRaises(DialogError):
            machine.transition(DialogState.IDLE, reason="not_a_whitelisted_reason")
        self.assertIs(machine.state, DialogState.LISTENING)

    def test_recover_rotates_window_identity_and_is_only_valid_from_fail_closed(self) -> None:
        machine = self.machine()
        original = machine.window_id
        with self.assertRaises(DialogError):
            machine.recover(reason="recovered")
        machine.fail_closed(reason="audio_failed")
        machine.recover(reason="recovered")
        self.assertIs(machine.state, DialogState.IDLE)
        self.assertNotEqual(machine.window_id, original)

    def test_window_open_issues_new_identity_and_resets_turn_state(self) -> None:
        clock = _Clock()
        window = DialogWindow(config=DialogWindowConfig(), now=clock)
        self.assertIs(window.state, DialogWindowState.CLOSED)
        self.assertFalse(window.is_open())
        status = window.open()
        self.assertIs(status.state, DialogWindowState.OPEN)
        self.assertEqual(status.turn_count, 0)
        first_identity = window.window_id
        window.begin_turn()
        self.assertEqual(window.status.turn_count, 1)
        window.close(reason=DialogWindowCloseReason.USER_CANCELLED)
        self.assertFalse(window.status.expired)
        reopened = window.open()
        self.assertNotEqual(reopened.window_id, first_identity)
        self.assertEqual(reopened.turn_count, 0)
        self.assertIsNone(reopened.close_reason)
        self.assertFalse(reopened.expired)

    def test_idle_and_window_timeouts_use_injected_clock_with_fixed_precedence(self) -> None:
        clock = _Clock()
        window = DialogWindow(
            config=DialogWindowConfig(idle_timeout_seconds=10, max_window_seconds=1000),
            now=clock,
        )
        window.open()
        clock.advance(9)
        self.assertIs(window.check_timeouts().state, DialogWindowState.OPEN)
        clock.advance(1)
        idle = window.check_timeouts()
        self.assertIs(idle.close_reason, DialogWindowCloseReason.IDLE_TIMEOUT)
        self.assertTrue(idle.expired)

        clock2 = _Clock()
        both = DialogWindow(
            config=DialogWindowConfig(idle_timeout_seconds=10, max_window_seconds=20),
            now=clock2,
        )
        both.open()
        clock2.advance(25)
        expired = both.check_timeouts()
        self.assertIs(expired.close_reason, DialogWindowCloseReason.MAX_WINDOW_DURATION)
        self.assertTrue(expired.expired)

    def test_max_turns_caps_turn_count_and_closes(self) -> None:
        window = self.open_window(max_turns=2)
        window.begin_turn()
        window.begin_turn()
        status = window.begin_turn()
        self.assertIs(status.state, DialogWindowState.CLOSED)
        self.assertIs(status.close_reason, DialogWindowCloseReason.MAX_TURNS)
        self.assertEqual(status.turn_count, 2)
        self.assertTrue(status.expired)

    def test_closed_window_never_revives_nor_rewrites_reason(self) -> None:
        window = self.open_window()
        window.begin_turn()
        window.close(reason=DialogWindowCloseReason.IDLE_TIMEOUT)
        identity = window.window_id
        turns = window.status.turn_count
        for call in (window.note_activity, window.begin_turn, window.end_turn, window.check_timeouts):
            with self.subTest(call=call.__name__):
                status = call()
                self.assertIs(status.state, DialogWindowState.CLOSED)
                self.assertIs(status.close_reason, DialogWindowCloseReason.IDLE_TIMEOUT)
                self.assertEqual(status.window_id, identity)
                self.assertEqual(status.turn_count, turns)

    def test_window_close_rejects_free_text_and_wrong_types(self) -> None:
        window = self.open_window()
        with self.assertRaises(DialogError):
            window.close(reason="please stop now")  # type: ignore[arg-type]
        # task-1 修正后：合法枚举值的字符串（如 "idle_timeout"）同样必须被拒，不做隐式强转。
        with self.assertRaises(DialogError):
            window.close(reason="idle_timeout")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            window.close(reason=7)  # type: ignore[arg-type]
        self.assertIs(window.state, DialogWindowState.OPEN)
        self.assertIsNone(window.close_reason)

    def test_turn_claim_source_must_be_an_enum_instance(self) -> None:
        # task-1 修正后：字符串 source（含 "asr"）抛 TypeError，不做隐式强转。
        with self.assertRaises(TypeError):
            TurnClaim(
                claim_id="claim-1",
                window_id="dialog-window-1",
                turn_id="turn-1",
                source="asr",  # type: ignore[arg-type]
                layer="voice",
                issued_at="2026-09-21T08:00:00Z",
            )
        accepted = TurnClaim(
            claim_id="claim-1",
            window_id="dialog-window-1",
            turn_id="turn-1",
            source=VoiceInputSource.ASR,
            layer="voice",
            issued_at="2026-09-21T08:00:00Z",
        )
        self.assertIs(accepted.source, VoiceInputSource.ASR)
        passive = TurnClaim(
            claim_id="claim-2",
            window_id="dialog-window-1",
            turn_id="turn-2",
            source=None,
            layer="voice",
            issued_at="2026-09-21T08:00:00Z",
        )
        self.assertIsNone(passive.source)

    def test_request_input_refuses_guard_states_without_migrating(self) -> None:
        def scenario(kind: str) -> tuple[DialogCoordinator, DialogStateMachine, DialogWindow]:
            machine = self.machine()
            window = self.open_window()
            coordinator = DialogCoordinator(machine=machine, window=window)
            machine.begin_text_turn()
            if kind == "authorization":
                coordinator.suspend_for_authorization(reason="authorization_required")
            elif kind == "execution":
                coordinator.suspend_for_execution(reason="execution_requested")
            elif kind == "fail_closed":
                machine.fail_closed(reason="audio_failed")
            else:
                machine.close(reason="shutdown")
            return coordinator, machine, window

        for kind in ("authorization", "execution", "fail_closed", "closed"):
            with self.subTest(kind=kind):
                coordinator, machine, window = scenario(kind)
                before_state = machine.state
                before_transition_count = len(machine.transitions)
                before_window = window.status
                permit = coordinator.request_input(layer="voice", source=VoiceInputSource.ASR)
                self.assertFalse(permit.granted)
                self.assertNotEqual(permit.reason, "granted")
                self.assertIs(machine.state, before_state)
                self.assertEqual(len(machine.transitions), before_transition_count)
                self.assertEqual(window.status, before_window)

    def test_second_claim_is_refused_and_release_is_single_shot(self) -> None:
        machine = self.machine()
        window = self.open_window()
        coordinator = DialogCoordinator(machine=machine, window=window)
        first = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertTrue(first.granted)
        second = coordinator.claim_turn(layer="text", source=VoiceInputSource.TEXT_SHELL)
        self.assertFalse(second.granted)
        self.assertIsNone(second.claim)
        self.assertNotEqual(second.reason, "granted")
        coordinator.release_turn(first.claim, reason="turn_completed")
        with self.assertRaises(DialogError):
            coordinator.release_turn(first.claim, reason="turn_completed")
        with self.assertRaises(DialogError):
            coordinator.release_turn(first.claim, reason="please stop now")
        self.assertIsNone(coordinator.active_claim)

    def test_forged_claim_is_rejected_without_disturbing_active_claim(self) -> None:
        machine = self.machine()
        window = self.open_window()
        coordinator = DialogCoordinator(machine=machine, window=window)
        granted = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        forged = TurnClaim(
            claim_id="claim-forged",
            window_id=window.window_id,
            turn_id="turn-forged",
            source=VoiceInputSource.ASR,
            layer="voice",
            issued_at="2026-09-21T08:00:00Z",
        )
        with self.assertRaises(DialogError):
            coordinator.note_speech_started(claim=forged)
        with self.assertRaises(DialogError):
            coordinator.note_speech_finished(claim=forged)
        with self.assertRaises(DialogError):
            coordinator.release_turn(forged, reason="turn_completed")
        self.assertEqual(coordinator.active_claim, granted.claim)
        self.assertIs(machine.state, DialogState.IDLE)

    def test_stale_window_claim_dies_after_reopen(self) -> None:
        clock = _Clock()
        machine = self.machine()
        window = self.open_window(now=clock)
        coordinator = DialogCoordinator(machine=machine, window=window)
        granted = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertIsNotNone(granted.claim)
        clock.advance(400)
        window.check_timeouts()
        self.assertFalse(window.is_open())
        self.assertIsNone(coordinator.active_claim)
        window.open()
        self.assertIsNone(coordinator.active_claim)
        self.assertNotEqual(granted.claim.window_id, window.window_id)
        with self.assertRaises(DialogError):
            coordinator.note_speech_started(claim=granted.claim)

    def test_request_input_reports_window_closed_and_invalidate_blocks_input(self) -> None:
        clock = _Clock()
        machine = self.machine()
        window = self.open_window(now=clock, idle_timeout_seconds=10)
        coordinator = DialogCoordinator(machine=machine, window=window)
        granted = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertTrue(granted.granted)
        coordinator.invalidate(reason="cancelled")
        self.assertIsNone(coordinator.active_claim)
        permit = coordinator.request_input(layer="voice", source=VoiceInputSource.ASR)
        self.assertFalse(permit.granted)
        self.assertIs(machine.state, DialogState.FAIL_CLOSED)
        clock.advance(11)
        window.check_timeouts()
        closed_permit = coordinator.request_input(layer="voice", source=VoiceInputSource.ASR)
        self.assertFalse(closed_permit.granted)
        self.assertEqual(closed_permit.reason, "window_closed")

    def test_layer_and_source_rules_are_enforced(self) -> None:
        machine = self.machine()
        window = self.open_window()
        coordinator = DialogCoordinator(machine=machine, window=window)
        with self.assertRaises(DialogError):
            coordinator.request_input(layer="tool")
        with self.assertRaises(DialogError):
            coordinator.claim_turn(layer="voice", source=VoiceInputSource.TEXT_SHELL)
        with self.assertRaises(DialogError):
            coordinator.claim_turn(layer="text", source=VoiceInputSource.ASR)
        legitimate = coordinator.claim_turn(layer="text", source=VoiceInputSource.TEXT_SHELL)
        self.assertTrue(legitimate.granted)
        self.assertEqual(legitimate.claim.layer, "text")

    def test_suspend_retires_claim_and_resume_requires_open_window(self) -> None:
        machine = self.machine()
        window = self.open_window()
        coordinator = DialogCoordinator(machine=machine, window=window)
        granted = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertTrue(granted.granted)
        coordinator.note_speech_started(claim=granted.claim)
        self.assertIs(machine.state, DialogState.USER_SPEAKING)
        coordinator.note_speech_finished(claim=granted.claim)
        self.assertIs(machine.state, DialogState.TRANSCRIBING)
        machine.transition(DialogState.THINKING, reason="transcription_ready")
        coordinator.suspend_for_authorization(reason="authorization_required")
        self.assertIs(machine.state, DialogState.AWAITING_AUTHORIZATION)
        self.assertIsNone(coordinator.active_claim)
        self.assertFalse(
            coordinator.request_input(layer="voice", source=VoiceInputSource.ASR).granted
        )
        # §2.1 规定 AWAITING_AUTHORIZATION 只能取消/恢复到 IDLE，不能自行继续。
        with self.assertRaises(DialogError):
            coordinator.resume_listening(reason="listening_started")
        machine.transition(DialogState.IDLE, reason="cancelled")
        coordinator.resume_listening(reason="listening_started")
        self.assertIs(machine.state, DialogState.LISTENING)

        second = coordinator.claim_turn(layer="voice", source=VoiceInputSource.ASR)
        self.assertTrue(second.granted)
        machine.transition(DialogState.THINKING, reason="transcription_ready")
        coordinator.suspend_for_execution(reason="execution_requested")
        self.assertIs(machine.state, DialogState.EXECUTING)
        self.assertIsNone(coordinator.active_claim)

        machine.fail_closed(reason="audio_failed")
        machine.recover(reason="recovered")
        window.close(reason=DialogWindowCloseReason.SHUTDOWN)
        with self.assertRaises(DialogError):
            coordinator.resume_listening(reason="listening_started")


class P9AuditAutoCaptureSemanticsTests(unittest.TestCase):
    """规格 §5 硬约束的独立行为断言（含对抗性输入）。"""

    def test_frames_stay_out_of_vad_until_speech_start_then_preroll_replays_in_order(self) -> None:
        seen: list[int] = []

        def detector(packet: AudioPacket) -> bool:
            seen.append(packet.frame.event_sequence)
            return packet.payload[:2] != b"\x00\x00"

        session = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=60, frame_duration_ms=20),
                speech_start=SpeechStartConfig(speech_frame_threshold=2, min_rms_dbfs=-40.0),
            ),
            detector=detector,
        )
        session.listen()
        session.observe(_AudioFixture.quiet(1))
        session.observe(_AudioFixture.quiet(2))
        self.assertEqual(seen, [], "语音起始前不得调用 VAD 检测器")
        self.assertIs(session.state, AutoCaptureState.LISTENING)
        self.assertEqual(session.buffered_frames, 2)

        third = session.observe(_AudioFixture.loud(3))
        self.assertFalse(third.speech_started)
        self.assertEqual(seen, [], "未达起始门槛前不得进入 VadSession")

        fourth = session.observe(_AudioFixture.loud(4))
        self.assertTrue(fourth.speech_started)
        self.assertIs(session.state, AutoCaptureState.CAPTURING)
        self.assertEqual(session.buffered_frames, 0)
        self.assertEqual(seen, [1, 2, 3, 4], "pre-roll 必须按原顺序回放")
        self.assertEqual(session.captured_frames, 4)

        session.observe(_AudioFixture.quiet(5))
        session.observe(_AudioFixture.quiet(6))
        ended = session.observe(_AudioFixture.quiet(7))
        self.assertIs(ended.state, AutoCaptureState.ENDED)
        self.assertIs(ended.reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        emitted = session.deliver()
        self.assertEqual([packet.frame.event_sequence for packet in emitted], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(session.deliver(), (), "deliver() 只能成功一次")

    def test_passive_buffer_is_bounded_and_drops_oldest_frames(self) -> None:
        bounded = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=100000, frame_duration_ms=20),
                passive_capacity_frames=3,
                speech_start=SpeechStartConfig(speech_frame_threshold=99, min_rms_dbfs=-40.0),
            ),
            detector=lambda packet: True,
        )
        bounded.listen()
        for sequence in range(1, 6):
            decision = bounded.observe(_AudioFixture.quiet(sequence))
            self.assertLessEqual(decision.buffered_frames, 3)
        self.assertEqual(bounded.buffered_frames, 3)
        self.assertIs(bounded.state, AutoCaptureState.LISTENING)

        session = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=100000, frame_duration_ms=20),
                passive_capacity_frames=3,
                speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-40.0),
            ),
            detector=lambda packet: True,
        )
        session.listen()
        for sequence in range(1, 6):
            session.observe(_AudioFixture.quiet(sequence))
        started = session.observe(_AudioFixture.loud(6))
        self.assertTrue(started.speech_started)
        self.assertEqual(session.captured_frames, 3, "pre-roll 只保留有界窗口内的最新帧")
        finished = session.finish()
        self.assertEqual(
            [packet.frame.event_sequence for packet in finished.emitted], [4, 5, 6]
        )

    def test_stale_boundary_and_discontinuity_fail_closed_before_delivery(self) -> None:
        cases = (
            ({"audio_session_id": "audio-old"}, AutoCaptureEndReason.STALE_AUDIO_SESSION),
            ({"provider_generation": 2}, AutoCaptureEndReason.STALE_GENERATION),
            ({"stream_id": "stream-old"}, AutoCaptureEndReason.STALE_STREAM),
            ({"device_epoch": 2}, AutoCaptureEndReason.STALE_DEVICE_EPOCH),
        )
        for overrides, expected in cases:
            with self.subTest(expected=expected.value):
                session = AutoCaptureSession(
                    bus=_AudioFixture.bus(),
                    config=AutoCaptureConfig(vad=VadConfig()),
                )
                session.listen()
                result = session.observe(_AudioFixture.quiet(1, **overrides))
                self.assertIs(result.state, AutoCaptureState.FAILED)
                self.assertIs(result.reason, expected)
                self.assertEqual(session.deliver(), ())

        gapped = AutoCaptureSession(
            bus=_AudioFixture.bus(), config=AutoCaptureConfig(vad=VadConfig())
        )
        gapped.listen()
        gapped.observe(_AudioFixture.quiet(1))
        gap = gapped.observe(_AudioFixture.quiet(3))
        self.assertIs(gap.reason, AutoCaptureEndReason.FRAME_DISCONTINUITY)
        self.assertEqual(gapped.deliver(), ())

        duplicate = AutoCaptureSession(
            bus=_AudioFixture.bus(), config=AutoCaptureConfig(vad=VadConfig())
        )
        duplicate.listen()
        duplicate.observe(_AudioFixture.quiet(1))
        repeated = duplicate.observe(_AudioFixture.quiet(1))
        self.assertIs(repeated.reason, AutoCaptureEndReason.FRAME_DISCONTINUITY)

    def test_stale_frame_after_speech_start_discards_captured_audio(self) -> None:
        session = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=100000, frame_duration_ms=20),
                speech_start=SpeechStartConfig(speech_frame_threshold=1),
            ),
            detector=lambda packet: True,
        )
        session.listen()
        started = session.observe(_AudioFixture.speech(1))
        self.assertIs(started.state, AutoCaptureState.CAPTURING)
        self.assertEqual(session.captured_frames, 1)
        stale = session.observe(_AudioFixture.quiet(2, device_epoch=2))
        self.assertIs(stale.state, AutoCaptureState.FAILED)
        self.assertIs(stale.reason, AutoCaptureEndReason.STALE_DEVICE_EPOCH)
        self.assertEqual(session.captured_frames, 0)
        self.assertEqual(session.deliver(), ())

    def test_speech_start_timeout_ends_without_delivery(self) -> None:
        session = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(),
                speech_start=SpeechStartConfig(speech_frame_threshold=99),
                speech_start_timeout_ms=40,
            ),
        )
        session.listen()
        session.observe(_AudioFixture.quiet(1))
        timed_out = session.observe(_AudioFixture.quiet(2))
        self.assertIs(timed_out.state, AutoCaptureState.ENDED)
        self.assertIs(timed_out.reason, AutoCaptureEndReason.SPEECH_START_TIMEOUT)
        self.assertFalse(session.speech_started)
        self.assertEqual(session.deliver(), ())

    def test_max_utterance_duration_truncates_within_the_cap(self) -> None:
        session = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=100000, frame_duration_ms=20),
                speech_start=SpeechStartConfig(speech_frame_threshold=1),
                max_utterance_ms=40,
            ),
            detector=lambda packet: True,
        )
        session.listen()
        session.observe(_AudioFixture.speech(1))
        truncated = session.observe(_AudioFixture.speech(2))
        self.assertIs(truncated.state, AutoCaptureState.ENDED)
        self.assertIs(truncated.reason, AutoCaptureEndReason.MAX_UTTERANCE_DURATION)
        self.assertEqual(session.captured_frames, 2)
        emitted = session.deliver()
        self.assertEqual(len(emitted), 2)
        total_ms = sum(packet.frame.duration_ms for packet in emitted)
        self.assertLessEqual(total_ms, 40)

    def test_invalidate_clears_everything_and_deliver_stays_empty(self) -> None:
        session = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=100000, frame_duration_ms=20),
                speech_start=SpeechStartConfig(speech_frame_threshold=1),
            ),
            detector=lambda packet: True,
        )
        session.listen()
        session.observe(_AudioFixture.speech(1))
        invalidated = session.invalidate(reason="cancelled")
        self.assertIs(invalidated.state, AutoCaptureState.FAILED)
        self.assertIs(session.state, AutoCaptureState.FAILED)
        self.assertEqual(session.terminal_reason, AutoCaptureEndReason.INVALIDATED)
        self.assertEqual(session.buffered_frames, 0)
        self.assertEqual(session.captured_frames, 0)
        self.assertEqual(session.deliver(), ())

    def test_observe_outside_listening_or_capturing_is_rejected_without_migration(self) -> None:
        idle = AutoCaptureSession(
            bus=_AudioFixture.bus(), config=AutoCaptureConfig(vad=VadConfig())
        )
        before = idle.state
        rejected = idle.observe(_AudioFixture.quiet(1))
        self.assertIs(rejected.state, before)
        self.assertIs(idle.state, before)
        self.assertFalse(rejected.emitted)
        self.assertIs(rejected.reason, AutoCaptureEndReason.INVALIDATED)

        invalidated = AutoCaptureSession(
            bus=_AudioFixture.bus(), config=AutoCaptureConfig(vad=VadConfig())
        )
        invalidated.listen()
        invalidated.invalidate(reason="cancelled")
        after = invalidated.observe(_AudioFixture.quiet(1))
        self.assertIs(after.state, AutoCaptureState.FAILED)
        self.assertIs(invalidated.state, AutoCaptureState.FAILED)
        self.assertFalse(after.emitted)
        self.assertIs(after.reason, AutoCaptureEndReason.SESSION_INVALIDATED)

    def test_rejection_reason_codes_distinguish_terminal_paths(self) -> None:
        # finish() 之后（外部正常关闭）：SESSION_CLOSED，且不迁移、不改写 terminal_reason。
        finished = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=100000, frame_duration_ms=20),
                speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-40.0),
            ),
            detector=lambda packet: True,
        )
        finished.listen()
        finished.observe(_AudioFixture.loud(1))
        finished.observe(_AudioFixture.loud(2))
        finished.finish()
        self.assertIs(finished.terminal_reason, None)
        for call in (
            lambda: finished.observe(_AudioFixture.quiet(3)),
            lambda: finished.listen(),
            lambda: finished.finish(),
        ):
            decision = call()
            self.assertIs(decision.state, AutoCaptureState.ENDED)
            self.assertIs(decision.reason, AutoCaptureEndReason.SESSION_CLOSED)
        self.assertIs(finished.terminal_reason, None)
        self.assertEqual(len(finished.deliver()), 2, "被拒的 observe 不得作废已捕获帧")

        # 内部终止（静音结束）：SESSION_ENDED_INTERNAL。
        timed_out = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=40, frame_duration_ms=20),
                speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-40.0),
            ),
            detector=lambda packet: packet.payload[:2] != b"\x00\x00",
        )
        timed_out.listen()
        timed_out.observe(_AudioFixture.loud(1))
        timed_out.observe(_AudioFixture.quiet(2))
        ended = timed_out.observe(_AudioFixture.quiet(3))
        self.assertIs(ended.reason, AutoCaptureEndReason.SILENCE_TIMEOUT)
        late = timed_out.observe(_AudioFixture.quiet(4))
        self.assertIs(late.state, AutoCaptureState.ENDED)
        self.assertIs(late.reason, AutoCaptureEndReason.SESSION_ENDED_INTERNAL)
        self.assertIs(timed_out.terminal_reason, AutoCaptureEndReason.SILENCE_TIMEOUT)

        # 失败终止：SESSION_ENDED_INTERNAL，且既有 terminal_reason 不被改写。
        failed = AutoCaptureSession(
            bus=_AudioFixture.bus(), config=AutoCaptureConfig(vad=VadConfig())
        )
        failed.listen()
        failed.observe(_AudioFixture.quiet(1, stream_id="stream-old"))
        self.assertIs(failed.state, AutoCaptureState.FAILED)
        self.assertIs(failed.terminal_reason, AutoCaptureEndReason.STALE_STREAM)
        late_failed = failed.observe(_AudioFixture.quiet(2))
        self.assertIs(late_failed.state, AutoCaptureState.FAILED)
        self.assertIs(late_failed.reason, AutoCaptureEndReason.SESSION_ENDED_INTERNAL)
        self.assertIs(failed.terminal_reason, AutoCaptureEndReason.STALE_STREAM)

    def test_detector_unavailable_and_failure_never_deliver(self) -> None:
        unavailable = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(),
                speech_start=SpeechStartConfig(speech_frame_threshold=1),
            ),
        )
        unavailable.listen()
        missing = unavailable.observe(_AudioFixture.speech(1))
        self.assertIs(missing.state, AutoCaptureState.FAILED)
        self.assertIs(missing.reason, AutoCaptureEndReason.DETECTOR_UNAVAILABLE)
        self.assertEqual(unavailable.deliver(), ())

        for label, detector in (
            ("non_boolean", lambda packet: 1),
            ("raises", lambda packet: (_ for _ in ()).throw(RuntimeError("vad"))),
        ):
            with self.subTest(detector=label):
                session = AutoCaptureSession(
                    bus=_AudioFixture.bus(),
                    config=AutoCaptureConfig(
                        vad=VadConfig(),
                        speech_start=SpeechStartConfig(speech_frame_threshold=1),
                    ),
                    detector=detector,  # type: ignore[arg-type]
                )
                session.listen()
                failed = session.observe(_AudioFixture.speech(1))
                self.assertIs(failed.state, AutoCaptureState.FAILED)
                self.assertIs(failed.reason, AutoCaptureEndReason.DETECTOR_FAILURE)
                self.assertEqual(session.deliver(), ())

    def test_finish_ends_without_speech_and_delivers_nothing(self) -> None:
        session = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(),
                speech_start=SpeechStartConfig(speech_frame_threshold=99),
            ),
        )
        session.listen()
        session.observe(_AudioFixture.quiet(1))
        finished = session.finish()
        self.assertIs(finished.state, AutoCaptureState.ENDED)
        self.assertIsNone(finished.reason)
        self.assertEqual(finished.emitted, ())
        self.assertFalse(session.speech_started)
        self.assertEqual(session.deliver(), ())
        closed = session.observe(_AudioFixture.quiet(2))
        self.assertIs(closed.reason, AutoCaptureEndReason.SESSION_CLOSED)
        self.assertIs(session.state, AutoCaptureState.ENDED)
        self.assertIsNone(session.terminal_reason)

    def test_finish_keeps_captured_frames_deliverable_exactly_once(self) -> None:
        session = AutoCaptureSession(
            bus=_AudioFixture.bus(),
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=100000, frame_duration_ms=20),
                speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-40.0),
            ),
            detector=lambda packet: True,
        )
        session.listen()
        session.observe(_AudioFixture.loud(1))
        session.observe(_AudioFixture.loud(2))
        self.assertIs(session.state, AutoCaptureState.CAPTURING)
        finished = session.finish()
        self.assertIs(finished.state, AutoCaptureState.ENDED)
        self.assertIsNone(finished.reason)
        self.assertIs(session.terminal_reason, None)
        self.assertEqual([packet.frame.event_sequence for packet in finished.emitted], [1, 2])
        delivered = session.deliver()
        self.assertEqual(delivered, finished.emitted)
        self.assertEqual(session.deliver(), ())
        # 会话自此的拒绝原因码固定为 SESSION_CLOSED，且不迁移状态。
        late = session.observe(_AudioFixture.quiet(3))
        self.assertIs(late.reason, AutoCaptureEndReason.SESSION_CLOSED)
        self.assertIs(session.state, AutoCaptureState.ENDED)
        self.assertIsNone(session.terminal_reason)

    def test_cancel_is_idempotent_and_closes_bus_when_still_open(self) -> None:
        bus = _AudioFixture.bus()
        session = AutoCaptureSession(
            bus=bus,
            config=AutoCaptureConfig(
                vad=VadConfig(silence_duration_ms=100000, frame_duration_ms=20),
                speech_start=SpeechStartConfig(speech_frame_threshold=1, min_rms_dbfs=-40.0),
            ),
            detector=lambda packet: True,
        )
        session.listen()
        session.observe(_AudioFixture.loud(1))
        finished = session.finish()
        self.assertIs(session.state, AutoCaptureState.ENDED)
        self.assertIsNone(session.terminal_reason)
        self.assertIs(bus.state, AudioBusState.OPEN)
        self.assertEqual(session.captured_frames, len(finished.emitted))

        first = session.cancel()
        self.assertIs(first.state, AutoCaptureState.ENDED)
        self.assertIsNone(first.reason)
        self.assertIs(bus.state, AudioBusState.CLOSED)
        self.assertEqual(bus.terminal_reason, AutoCaptureEndReason.CANCELLED.value)
        self.assertIsNone(session.terminal_reason, "cancel() 不得改写既有终止原因")
        self.assertEqual(session.captured_frames, 0, "cancel() 必须作废未交付帧")
        self.assertEqual(session.deliver(), ())

        second = session.cancel()
        self.assertIs(second.state, AutoCaptureState.ENDED)
        self.assertIsNone(second.reason)
        self.assertIs(bus.state, AudioBusState.CLOSED)
        self.assertIsNone(session.terminal_reason)
        self.assertEqual(session.deliver(), ())

    def test_cancel_in_failed_state_closes_bus_and_keeps_existing_terminal_reason(self) -> None:
        bus = _AudioFixture.bus()
        session = AutoCaptureSession(bus=bus, config=AutoCaptureConfig(vad=VadConfig()))
        session.listen()
        session.observe(_AudioFixture.quiet(1, stream_id="stream-old"))
        self.assertIs(session.state, AutoCaptureState.FAILED)
        self.assertIs(session.terminal_reason, AutoCaptureEndReason.STALE_STREAM)
        self.assertIs(bus.state, AudioBusState.OPEN)

        cancelled = session.cancel()
        self.assertIs(cancelled.state, AutoCaptureState.FAILED, "cancel() 不得把 FAILED 迁移成其他状态")
        self.assertIs(cancelled.reason, AutoCaptureEndReason.STALE_STREAM)
        self.assertIs(session.state, AutoCaptureState.FAILED)
        self.assertIs(session.terminal_reason, AutoCaptureEndReason.STALE_STREAM)
        self.assertIs(bus.state, AudioBusState.CLOSED)
        self.assertEqual(bus.terminal_reason, AutoCaptureEndReason.CANCELLED.value)
        self.assertEqual(session.deliver(), ())

    def test_cancel_from_listening_or_capturing_fails_closed(self) -> None:
        bus = _AudioFixture.bus()
        session = AutoCaptureSession(bus=bus, config=AutoCaptureConfig(vad=VadConfig()))
        session.listen()
        session.observe(_AudioFixture.quiet(1))
        cancelled = session.cancel()
        self.assertIs(cancelled.state, AutoCaptureState.ENDED)
        self.assertIs(cancelled.reason, AutoCaptureEndReason.CANCELLED)
        self.assertIs(bus.state, AudioBusState.CLOSED)
        self.assertEqual(session.captured_frames, 0)
        self.assertEqual(session.deliver(), ())

    def test_speech_start_config_gate_and_latch(self) -> None:
        detector = SpeechStartDetector(
            config=SpeechStartConfig(speech_frame_threshold=2, min_rms_dbfs=-40.0)
        )
        first = detector.observe(_AudioFixture.loud(1))
        self.assertFalse(first.started)
        self.assertTrue(first.observed)
        self.assertEqual(detector.speech_frames, 1)
        second = detector.observe(_AudioFixture.loud(2))
        self.assertTrue(second.started)
        missed = detector.observe(_AudioFixture.quiet(3))
        self.assertFalse(missed.observed)
        self.assertTrue(missed.started, "起始判定必须锁存到 reset()")
        self.assertEqual(detector.speech_frames, 0, "reset_on_miss=True 时命中中断清零")
        self.assertEqual(detector.observed_frames, 3)
        detector.reset()
        self.assertFalse(detector.started)
        self.assertEqual(detector.speech_frames, 0)
        self.assertEqual(detector.observed_frames, 0)

    def test_audit_raw_evidence_counts(self) -> None:
        import importlib

        print("[audit] audited_sources=", [path.name for path in AUDITED_SOURCE_FILES])
        for path in AUDITED_SOURCE_FILES:
            tree = _tree(path)
            imports = _import_names(tree)
            attributes = _dotted_names(tree)
            safety = sorted(
                name
                for name in (imports | attributes)
                if any(name == prefix or name.startswith(f"{prefix}.") for prefix in SAFETY_MODULE_PREFIXES)
            )
            dangerous = sorted(_call_names(tree).intersection(DANGEROUS_CALL_NAMES))
            print(
                f"[audit] {path.name}: safety_refs={len(safety)} "
                f"dangerous_calls={len(dangerous)} imports={sorted(imports)}"
            )
        print("[audit] DIALOG_TRANSITION_REASONS size=", len(DIALOG_TRANSITION_REASONS))
        print("[audit] AutoCaptureEndReason size=", len(list(AutoCaptureEndReason)))
        print("[audit] AutoCaptureDecision fields=", [field.name for field in fields(AutoCaptureDecision)])
        print("[audit] dialog_module=", importlib.import_module("app.dialog").__name__)


if __name__ == "__main__":
    unittest.main()
