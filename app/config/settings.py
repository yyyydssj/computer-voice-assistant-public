"""P1 配置加载与本地私有目录边界。

本模块只使用 Python 标准库。它负责读取通用配置和未提交的本地
``.env``，不负责连接任何 Provider、创建 Agent 会话或执行电脑动作。
"""

from __future__ import annotations

import ipaddress
import os
import re
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping


class ConfigError(ValueError):
    """配置内容或配置路径违反安全约束。"""


_SENSITIVE_KEYS = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "VOLC_TTS_API_KEY",
        "VOLC_TTS_VOICE_ID",
        "OPENAI_API_KEY",
    }
)
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_SUBDIRECTORIES = ("logs", "plans", "audit", "temp")
ProviderName = Literal["local", "deepseek"]
VoiceStudioMode = Literal["disabled", "http_wav", "ws_pcm", "auto"]
_ALLOWED_PROVIDERS = ("local", "deepseek")
_ALLOWED_VOICESTUDIO_MODES = ("disabled", "http_wav", "ws_pcm", "auto")
_WS_URL_SCHEMES = frozenset({"ws", "wss"})
_HTTP_URL_SCHEMES = frozenset({"http", "https"})
# ``TTS_EDGE_VOICE`` 的字符集与长度规则必须与 ``app.tts.providers.edge_sapi.
# EdgeSapiConfig.voice_name`` 一致（本模块只依赖标准库，因此有意保留这份小型
# 重复规则，而不是在配置层导入 TTS provider）。空字符串表示 Edge 槽未配置。
_EDGE_VOICE_RE = re.compile(r"^[A-Za-z0-9 ()\-_.]+$")
_EDGE_VOICE_MAX_LENGTH = 128
# 真实 Provider 的双条件开关之一：``ASSISTANT_PROVIDER=deepseek`` 且显式 opt-in。
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})
# 允许从项目 .env 注入到当前进程环境的白名单；只在显式 opt-in 的真实 Provider 路径使用。
_INJECTABLE_SECRETS = frozenset({"DEEPSEEK_API_KEY"})


@dataclass(frozen=True)
class SensitiveConfig:
    """只记录敏感配置是否存在，不保存或展示其值。"""

    configured_keys: frozenset[str] = field(default_factory=frozenset)

    @property
    def configured(self) -> bool:
        return bool(self.configured_keys)

    def has(self, key: str) -> bool:
        return key in self.configured_keys


@dataclass(frozen=True)
class Settings:
    """运行时外壳所需的无副作用配置。

    ``project_root`` 是代码仓库位置；``data_root`` 默认位于仓库外，
    以避免日志、计划、审计和临时文件进入公开代码目录。
    """

    project_root: Path
    data_root: Path
    model_root: Path
    logs_root: Path
    plans_root: Path
    audit_root: Path
    temp_root: Path
    wake_word: str = "小饼"
    wake_sensitivity: float = 0.5
    confirm_timeout_sec: float = 10.0
    command_timeout_sec: float = 30.0
    provider: ProviderName = "local"
    deepseek_opt_in: bool = False
    persona_path: str = ""
    """人物模板路径（``ASSISTANT_PERSONA_PATH``）；空字符串 = 用仓库默认
    ``implementation_plan/persona.md``。显式给出时解析失败即启动失败，不静默换人设。"""
    tts_voicestudio_mode: VoiceStudioMode = "disabled"
    tts_edge_voice: str = ""
    tts_ws_url: str = "ws://127.0.0.1:3900/ws/tts"
    tts_http_generate_url: str = "http://127.0.0.1:3900/generate"
    tts_ws_connect_timeout_s: float = 2.0
    tts_sentence_timeout_s: float = 18.0
    tts_ws_frame_idle_timeout_s: float = 3.0
    tts_ws_session_idle_s: float = 30.0
    tts_pcm_start_buffer_ms: int = 160
    tts_pcm_max_queued_audio_ms: int = 8000
    allowed_root: Path | None = None
    default_work_dir: Path | None = None
    sensitive: SensitiveConfig = field(default_factory=SensitiveConfig)
    dotenv_path: Path | None = None

    def __post_init__(self) -> None:
        if type(self.provider) is not str or self.provider not in _ALLOWED_PROVIDERS:
            raise ConfigError("provider must be local or deepseek")
        if type(self.deepseek_opt_in) is not bool:
            raise ConfigError("deepseek_opt_in 必须是布尔值")
        if type(self.persona_path) is not str:
            raise ConfigError("persona_path 必须是字符串（空表示使用仓库默认人设）")
        if type(self.tts_voicestudio_mode) is not str or self.tts_voicestudio_mode not in _ALLOWED_VOICESTUDIO_MODES:
            raise ConfigError("TTS_VOICESTUDIO_MODE must be disabled, http_wav, ws_pcm or auto")
        _edge_voice_name("TTS_EDGE_VOICE", self.tts_edge_voice)
        _loopback_url("tts_ws_url", self.tts_ws_url, _WS_URL_SCHEMES)
        _loopback_url("tts_http_generate_url", self.tts_http_generate_url, _HTTP_URL_SCHEMES)
        for name, value in (
            ("tts_ws_connect_timeout_s", self.tts_ws_connect_timeout_s),
            ("tts_sentence_timeout_s", self.tts_sentence_timeout_s),
            ("tts_ws_frame_idle_timeout_s", self.tts_ws_frame_idle_timeout_s),
            ("tts_ws_session_idle_s", self.tts_ws_session_idle_s),
        ):
            if type(value) not in {int, float} or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{name} 必须是正数")
        for name, value in (
            ("tts_pcm_start_buffer_ms", self.tts_pcm_start_buffer_ms),
            ("tts_pcm_max_queued_audio_ms", self.tts_pcm_max_queued_audio_ms),
        ):
            if type(value) is not int or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{name} 必须是正整数")

    def public_summary(self) -> dict[str, object]:
        """返回可安全用于 UI/日志的摘要，不包含任何 secret 原文。"""

        return {
            "data_root_configured": True,
            "model_root_configured": True,
            "logs_root_configured": True,
            "plans_root_configured": True,
            "audit_root_configured": True,
            "temp_root_configured": True,
            "wake_word_configured": bool(self.wake_word),
            "sensitive_keys_configured": sorted(self.sensitive.configured_keys),
            "provider": self.provider,
            "deepseek_opt_in": self.deepseek_opt_in,
            "tts_voicestudio_mode": self.tts_voicestudio_mode,
            "tts_edge_voice_configured": bool(self.tts_edge_voice),
            "tts_ws_url_loopback": bool(self.tts_ws_url),
            "tts_http_generate_url_loopback": bool(self.tts_http_generate_url),
            "tts_ws_connect_timeout_s": self.tts_ws_connect_timeout_s,
            "tts_sentence_timeout_s": self.tts_sentence_timeout_s,
            "tts_pcm_start_buffer_ms": self.tts_pcm_start_buffer_ms,
            "tts_pcm_max_queued_audio_ms": self.tts_pcm_max_queued_audio_ms,
            "provider_ready": False,
            "mode": "text",
        }

    def prepare_directories(self) -> None:
        """创建并验证本程序私有目录；不会创建模型或下载任何内容。"""

        _validate_private_data_root(self.project_root, self.data_root)
        _validate_runtime_directory_layout(
            self.project_root,
            self.data_root,
            logs_root=self.logs_root,
            plans_root=self.plans_root,
            audit_root=self.audit_root,
            temp_root=self.temp_root,
        )
        directories = (
            self.data_root,
            self.model_root,
            self.logs_root,
            self.plans_root,
            self.audit_root,
            self.temp_root,
        )
        for directory in directories:
            _ensure_directory_writable(directory)


def load_settings(
    *,
    project_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | str | None = None,
) -> Settings:
    """加载配置。

    优先级为：调用方 ``environ``/进程环境 > 项目根目录 ``.env`` > 默认值。
    ``.env`` 是本地私有输入，解析结果只保留配置所需值；未知键允许存在，
    但不会被写入摘要或日志。
    """

    root = _resolve_project_root(project_root)
    process_env = dict(os.environ if environ is None else environ)
    env_file = Path(dotenv_path) if dotenv_path is not None else root / ".env"
    dotenv_values = _read_dotenv(env_file) if env_file.is_file() else {}
    merged = {**dotenv_values, **process_env}

    data_root = _resolve_data_root(root, merged.get("ASSISTANT_DATA_ROOT"))
    model_root = _resolve_path(
        merged.get("ASSISTANT_MODEL_ROOT"),
        data_root / "models",
    )
    logs_root = _resolve_path(merged.get("ASSISTANT_LOGS_ROOT"), data_root / "logs")
    plans_root = _resolve_path(merged.get("ASSISTANT_PLANS_ROOT"), data_root / "plans")
    audit_root = _resolve_path(merged.get("ASSISTANT_AUDIT_ROOT"), data_root / "audit")
    temp_root = _resolve_path(merged.get("ASSISTANT_TEMP_ROOT"), data_root / "temp")

    _validate_private_data_root(root, data_root)
    _validate_local_path(model_root, field_name="模型目录")
    _validate_runtime_directory_layout(
        root,
        data_root,
        logs_root=logs_root,
        plans_root=plans_root,
        audit_root=audit_root,
        temp_root=temp_root,
    )

    sensitive = SensitiveConfig(
        frozenset(
            key
            for key in _SENSITIVE_KEYS
            if _has_nonempty_value(merged.get(key))
        )
    )

    return Settings(
        project_root=root,
        data_root=data_root,
        model_root=model_root,
        logs_root=logs_root,
        plans_root=plans_root,
        audit_root=audit_root,
        temp_root=temp_root,
        wake_word=_read_text(merged, "WAKE_WORD", "小饼"),
        wake_sensitivity=_read_float(merged, "WAKE_SENSITIVITY", 0.5, 0.0, 1.0),
        confirm_timeout_sec=_read_float(merged, "CONFIRM_TIMEOUT_SEC", 10.0, 0.1, 3600.0),
        command_timeout_sec=_read_float(merged, "COMMAND_TIMEOUT_SEC", 30.0, 0.1, 3600.0),
        provider=_read_provider(merged, "ASSISTANT_PROVIDER", "local"),
        deepseek_opt_in=_read_bool(merged, "ASSISTANT_DEEPSEEK_OPT_IN", False),
        # 2026-10-24：人设改成文件驱动。空 = 用仓库默认 implementation_plan/persona.md；
        # 显式给出（绝对路径，或相对项目根目录的路径）解析失败即启动失败。
        persona_path=_read_optional_text(merged, "ASSISTANT_PERSONA_PATH", ""),
        tts_voicestudio_mode=_read_voicestudio_mode(merged, "TTS_VOICESTUDIO_MODE", "disabled"),
        tts_edge_voice=_read_edge_voice(merged, "TTS_EDGE_VOICE", ""),
        tts_ws_url=_loopback_url("TTS_WS_URL", merged.get("TTS_WS_URL", "ws://127.0.0.1:3900/ws/tts"), _WS_URL_SCHEMES),
        tts_http_generate_url=_loopback_url(
            "TTS_HTTP_GENERATE_URL",
            merged.get("TTS_HTTP_GENERATE_URL", "http://127.0.0.1:3900/generate"),
            _HTTP_URL_SCHEMES,
        ),
        tts_ws_connect_timeout_s=_read_float(merged, "TTS_WS_CONNECT_TIMEOUT_S", 2.0, 0.05, 600.0),
        tts_sentence_timeout_s=_read_float(merged, "TTS_SENTENCE_TIMEOUT_S", 18.0, 0.1, 3600.0),
        tts_ws_frame_idle_timeout_s=_read_float(merged, "TTS_WS_FRAME_IDLE_TIMEOUT_S", 3.0, 0.05, 600.0),
        tts_ws_session_idle_s=_read_float(merged, "TTS_WS_SESSION_IDLE_S", 30.0, 1.0, 3600.0),
        tts_pcm_start_buffer_ms=_read_int(merged, "TTS_PCM_START_BUFFER_MS", 160, 1, 5000),
        tts_pcm_max_queued_audio_ms=_read_int(merged, "TTS_PCM_MAX_QUEUED_AUDIO_MS", 8000, 100, 600000),
        allowed_root=_read_optional_path(merged.get("ALLOWED_ROOT")),
        default_work_dir=_read_optional_path(merged.get("DEFAULT_WORK_DIR")),
        sensitive=sensitive,
        dotenv_path=env_file if env_file.is_file() else None,
    )


def _resolve_project_root(value: Path | str | None) -> Path:
    if value is not None:
        root = Path(value).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parents[2]
    if not root.is_dir():
        raise ConfigError(f"项目根目录不存在或不是目录: {root}")
    return root


def _resolve_data_root(project_root: Path, configured: str | None) -> Path:
    if configured:
        raw_path = Path(configured).expanduser()
        _validate_local_path(raw_path, field_name="数据根目录")
        return raw_path.resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "ComputerVoiceAssistant").resolve()
    return (Path.home() / ".computer-voice-assistant").resolve()


def _resolve_path(configured: str | None, default: Path) -> Path:
    if not configured:
        return default.resolve()
    raw_path = Path(configured).expanduser()
    _validate_local_path(raw_path, field_name="配置路径")
    return raw_path.resolve()


def _read_optional_path(value: str | None) -> Path | None:
    if not _has_nonempty_value(value):
        return None
    raw_path = Path(value).expanduser()
    _validate_local_path(raw_path, field_name="用户路径")
    return raw_path.resolve()


def _read_text(values: Mapping[str, str], key: str, default: str) -> str:
    value = values.get(key, default).strip()
    if not value:
        raise ConfigError(f"配置 {key} 不能为空")
    return value


def _read_optional_text(values: Mapping[str, str], key: str, default: str = "") -> str:
    """读取可缺省的文本配置；未设置或空白一律返回 ``default``（空字符串 = 未配置）。

    2026-10-24 引入：``ASSISTANT_PERSONA_PATH`` 允许不设——不设表示"用仓库默认人物模板"，
    与"显式设了一个空路径"是同一件事，不该让启动失败。
    """

    raw = values.get(key)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    return value


def _read_edge_voice(values: Mapping[str, str], key: str, default: str) -> str:
    """返回去除首尾空白的 Edge 声线名；缺失或空白时回落到默认（未配置）。"""

    raw = values.get(key)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    return _edge_voice_name(key, value)


def _edge_voice_name(key: str, value: object) -> str:
    """校验 Edge 槽声线名；空字符串表示未配置，绝不接受引号、换行或路径。"""

    if type(value) is not str:
        raise ConfigError(f"配置 {key} 必须是字符串")
    if not value:
        return value
    if len(value) > _EDGE_VOICE_MAX_LENGTH or not _EDGE_VOICE_RE.fullmatch(value):
        raise ConfigError(
            f"配置 {key} 只能包含字母、数字、空格、()、-、_、.，且长度不超过 {_EDGE_VOICE_MAX_LENGTH}"
        )
    return value


def _read_provider(values: Mapping[str, str], key: str, default: ProviderName) -> ProviderName:
    raw = values.get(key, default)
    value = raw.strip().lower()
    if value not in _ALLOWED_PROVIDERS:
        raise ConfigError(f"{key} must be local or deepseek")
    return value  # type: ignore[return-value]


def _read_bool(values: Mapping[str, str], key: str, default: bool) -> bool:
    """严格解析布尔配置：只接受 true/false/1/0/yes/no/on/off（大小写不敏感）。"""

    raw = values.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_TOKENS:
        return True
    if value in _FALSE_TOKENS:
        return False
    raise ConfigError(f"{key} 必须是 true/false/1/0/yes/no/on/off")


def inject_dotenv_secret(name: str, *, dotenv_path: Path) -> bool:
    """把 ``.env`` 中的单个**白名单**密钥注入当前进程环境，返回是否真的注入。

    设计约束（安全优先）：

    - 只接受白名单键，避免从 ``.env`` 注入任意变量；
    - 进程环境已有非空值时**不覆盖**（环境变量优先于本地文件）；
    - 只写内存，绝不打印、记录或返回密钥值；
    - 只读取一次性解析结果，不执行任何表达式。

    返回 ``False`` 表示"无需注入或没有可用值"，调用方无需据此报错。
    """

    if name not in _INJECTABLE_SECRETS:
        raise ConfigError(f"不允许从 .env 注入 {name}")
    path = Path(dotenv_path)
    existing = os.environ.get(name)
    if type(existing) is str and existing.strip():
        return False
    if not path.is_file():
        return False
    candidate = _read_dotenv(path).get(name)
    if type(candidate) is not str or not candidate.strip():
        return False
    os.environ[name] = candidate.strip()
    return True


def _read_voicestudio_mode(
    values: Mapping[str, str], key: str, default: VoiceStudioMode
) -> VoiceStudioMode:
    raw = values.get(key, default)
    value = raw.strip().lower()
    if value not in _ALLOWED_VOICESTUDIO_MODES:
        raise ConfigError(f"{key} must be disabled, http_wav, ws_pcm or auto")
    return value  # type: ignore[return-value]


def _read_int(
    values: Mapping[str, str],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"配置 {key} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"配置 {key} 超出范围 [{minimum}, {maximum}]")
    return value


def _loopback_url(name: str, value: object, schemes: frozenset[str]) -> str:
    """Accept only a loopback URL with no credentials, query or fragment."""

    if type(value) is not str or not value.strip():
        raise ConfigError(f"配置 {name} 不能为空")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in schemes or not parsed.hostname:
        raise ConfigError(f"配置 {name} 必须是 {'/'.join(sorted(schemes))} 本地 URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(f"配置 {name} 不得包含凭据、query 或 fragment")
    host = parsed.hostname.rstrip(".").casefold()
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ConfigError(f"配置 {name} 必须指向 loopback") from None
        if not address.is_loopback:
            raise ConfigError(f"配置 {name} 必须指向 loopback")
    try:
        port = parsed.port
    except ValueError:
        raise ConfigError(f"配置 {name} 端口非法") from None
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"配置 {name} 端口非法")
    return value


def _read_float(
    values: Mapping[str, str],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = values.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"配置 {key} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"配置 {key} 超出范围 [{minimum}, {maximum}]")
    return value


def _has_nonempty_value(value: str | None) -> bool:
    return value is not None and bool(value.strip())


def _validate_private_data_root(project_root: Path, data_root: Path) -> None:
    _validate_local_path(data_root, field_name="数据根目录")
    try:
        data_root.relative_to(project_root)
    except ValueError:
        return
    raise ConfigError("数据根目录不得位于项目仓库内")


def _validate_local_path(path: Path, *, field_name: str) -> None:
    raw = str(path)
    if raw.startswith(("\\\\", "//")):
        raise ConfigError(f"{field_name}不得使用 UNC 或网络路径")
    if not path.is_absolute():
        raise ConfigError(f"{field_name}必须是绝对路径")


def _validate_runtime_directory_layout(
    project_root: Path,
    data_root: Path,
    *,
    logs_root: Path,
    plans_root: Path,
    audit_root: Path,
    temp_root: Path,
) -> None:
    """确保运行目录属于当前数据根，且日志、计划、审计和临时数据相互隔离。"""

    named_directories = {
        "日志目录": logs_root,
        "计划目录": plans_root,
        "审计目录": audit_root,
        "临时目录": temp_root,
    }
    for field_name, path in named_directories.items():
        _validate_local_path(path, field_name=field_name)
        try:
            path.relative_to(project_root)
        except ValueError:
            pass
        else:
            raise ConfigError(f"{field_name}不得位于项目仓库内")
        try:
            relative = path.relative_to(data_root)
        except ValueError as exc:
            raise ConfigError(f"{field_name}必须位于数据根目录内") from exc
        if not relative.parts:
            raise ConfigError(f"{field_name}不能与数据根目录相同")

    items = tuple(named_directories.items())
    for index, (left_name, left_path) in enumerate(items):
        for right_name, right_path in items[index + 1 :]:
            if _paths_overlap(left_path, right_path):
                raise ConfigError(f"{left_name}与{right_name}必须相互隔离")


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _ensure_directory_writable(directory: Path) -> None:
    if directory.exists() and not directory.is_dir():
        raise ConfigError(f"配置目录不是目录: {directory}")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".write-check-",
            dir=directory,
            delete=True,
        ) as marker:
            marker.write(b"ok")
            marker.flush()
    except (OSError, PermissionError) as exc:
        raise ConfigError(f"配置目录不可写: {directory}") from exc


def _read_dotenv(path: Path) -> dict[str, str]:
    """读取有限的 dotenv 子集，不执行任何表达式或 shell 内容。"""

    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f".env 必须使用 UTF-8: {path}") from exc
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f".env 第 {line_number} 行缺少 '='")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            raise ConfigError(f".env 第 {line_number} 行变量名非法")
        values[key] = _unquote_dotenv_value(value.strip())
    return values


def _unquote_dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if " #" in value:
        value = value.split(" #", 1)[0]
    return value.strip()