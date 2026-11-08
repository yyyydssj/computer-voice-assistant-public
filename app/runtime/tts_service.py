"""本机 TTS 服务（VoiceStudio）可达性检查与**按需自动拉起**。

背景：``--voice`` / ``--voice --auto`` 的默认播报通道是 ``ws_pcm``，它必须先通过
``GET http://127.0.0.1:3900/profiles`` 的**身份校验**（档案 id/名字/参考音频哈希）。服务
没启动时该检查必然失败，语音入口会如实降级为文字——用户看到的就是
``provider profile verification failed``。

本模块只做三件事：
1. **探测**：服务是否已经可达（只读 GET，不写任何东西）；
2. **拉起**：不可达时用可配置命令把它作为**独立后台进程**启动（不阻塞、不接管它的输出）；
3. **等待**：轮询到超时为止，如实返回「已就绪 / 刚拉起 / 起不来」。

硬边界：
- 模块级只导入 stdlib；不联网（只访问 loopback 的私有 TTS 端口）；
- **不写死任何个人路径**：python 解释器 / 后端脚本 / 工作目录全部来自环境变量，
  未配置时给出明确错误而不是猜路径；
- 不用 ``shell=True``，不做字符串拼接执行；找不到解释器或脚本时**不启动**；
- 只在本进程**自己拉起**了服务时才允许 ``shutdown_owned()`` 关闭它；
  已经在你机器上跑着的服务绝不被本模块关掉。

环境变量（不写进仓库，属于本机配置）：
- ``ASSISTANT_TTS_AUTOSTART``：``1/true/on``（默认）或 ``0/false/off``；
- ``ASSISTANT_TTS_PYTHON``：启动用的 python 解释器路径；
- ``ASSISTANT_TTS_BACKEND``：TTS 后端入口脚本（如 ``...\\project\\backend\\main.py``）；
- ``ASSISTANT_TTS_CWD``：子进程工作目录（默认取后端脚本所在目录）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

TTS_BASE_URL = "http://127.0.0.1:3900"
"""本机私有 TTS 服务的 loopback 基址（与 ``voice_entry.VOICE_HTTP_BASE`` 一致）。"""

PROFILE_PROBE_PATH = "/profiles"
"""只读探测用的最小端点：``GET /profiles`` 返回档案列表（不含任何音频内容）。"""

DEFAULT_START_TIMEOUT_SECONDS = 90.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
PROBE_TIMEOUT_SECONDS = 5.0
"""单次探测超时；与 ``private_selection.verify_provider`` 的 5s 保持一致。"""

ENV_AUTOSTART = "ASSISTANT_TTS_AUTOSTART"
ENV_PYTHON = "ASSISTANT_TTS_PYTHON"
ENV_BACKEND = "ASSISTANT_TTS_BACKEND"
ENV_CWD = "ASSISTANT_TTS_CWD"

_FALSE_VALUES = frozenset({"0", "false", "off", "no"})
_TRUE_VALUES = frozenset({"1", "true", "on", "yes"})


class TtsServiceError(RuntimeError):
    """自动拉起的前置条件不满足（缺解释器/脚本、命令非法等）。

    ``str()`` 只带固定短代码，绝不携带个人路径内容以外的堆栈或系统正文。
    """


@dataclass(frozen=True, slots=True)
class TtsStartCommand:
    """拉起 TTS 服务的命令描述（argv 列表 + 工作目录），不做任何字符串拼接。"""

    argv: tuple[str, ...]
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise TtsServiceError("empty_command")
        for item in self.argv:
            if not isinstance(item, str) or not item:
                raise TtsServiceError("invalid_command")
        if self.cwd is not None and not isinstance(self.cwd, Path):
            raise TtsServiceError("invalid_cwd")


@dataclass(frozen=True, slots=True)
class TtsStartResult:
    """自动拉起的结果事实（只读、可打印、可入日志）。"""

    enabled: bool
    already_reachable: bool
    started: bool
    reachable: bool
    pid: int | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None
    command: tuple[str, ...] = ()

    def summary(self) -> str:
        """给用户看的一句话结论（不含任何路径以外的敏感正文）。"""

        if not self.enabled:
            return "已按配置关闭 TTS 自动启动。"
        if self.reachable and self.already_reachable:
            return "本机 TTS 服务已在运行。"
        if self.reachable and self.started:
            return f"已自动启动本机 TTS 服务（pid={self.pid}，用时 {self.elapsed_seconds:.1f}s）。"
        if self.error == "command_not_configured":
            return (
                "TTS 自动启动被跳过：未配置启动命令。请设置环境变量 "
                f"{ENV_PYTHON} 与 {ENV_BACKEND}（或改用 --tts-mode edge / none）。"
            )
        if self.error == "start_timeout":
            return (
                f"TTS 服务在 {self.elapsed_seconds:.1f}s 内未就绪（已启动 pid={self.pid}），"
                "将按设计降级为文字输入。"
            )
        if self.error is not None:
            return f"TTS 自动启动失败（{self.error}），将按设计降级。"
        return f"TTS 服务在 {self.elapsed_seconds:.1f}s 内未就绪。"


def autostart_enabled(environ: "os._Environ[str] | dict[str, str] | None" = None) -> bool:
    """是否允许自动拉起（默认允许；显式设为 0/false/off 时关闭）。"""

    source = os.environ if environ is None else environ
    raw = str(source.get(ENV_AUTOSTART, "")).strip().lower()
    if not raw:
        return True
    if raw in _FALSE_VALUES:
        return False
    if raw in _TRUE_VALUES:
        return True
    # 无法识别的取值按“不自动启动”处理：宁可少做副作用。
    return False


def build_start_command(
    environ: "os._Environ[str] | dict[str, str] | None" = None,
    *,
    override: Sequence[str] | None = None,
) -> TtsStartCommand | None:
    """由环境变量（或显式 override）构造启动命令；缺配置时返回 ``None``。"""

    if override is not None:
        argv = tuple(str(item) for item in override)
        if not argv:
            return None
        return TtsStartCommand(argv=argv, cwd=None)
    source = os.environ if environ is None else environ
    python = str(source.get(ENV_PYTHON, "")).strip()
    backend = str(source.get(ENV_BACKEND, "")).strip()
    if not python or not backend:
        return None
    backend_path = Path(backend)
    cwd_raw = str(source.get(ENV_CWD, "")).strip()
    cwd = Path(cwd_raw) if cwd_raw else backend_path.parent
    return TtsStartCommand(argv=(python, backend), cwd=cwd)


def _http_reachable(
    base_url: str = TTS_BASE_URL,
    *,
    path: str = PROFILE_PROBE_PATH,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    opener: Callable[..., object] | None = None,
) -> bool:
    """只读探测：``GET {base_url}{path}`` 返回 200 且是 JSON 列表即视为可达。"""

    open_fn = opener if opener is not None else urllib.request.urlopen
    try:
        with open_fn(base_url + path, timeout=timeout) as response:  # type: ignore[attr-defined]
            if getattr(response, "status", 0) != 200:
                return False
            payload = response.read(256_001)  # type: ignore[attr-defined]
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        return False
    if len(payload) > 256_000:
        return False
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, list)


def _spawn(command: TtsStartCommand) -> int | None:
    """把服务作为独立后台进程启动，返回 pid；失败返回 ``None``（不抛异常给语音链路）。

    刻意不接管子进程的 stdout/stderr：它是用户自己的本机服务，日志归它自己管；
    也不等待它结束（``Popen`` 立即返回），更不用 ``shell=True``。
    """

    try:
        process = subprocess.Popen(  # noqa: S603 - argv 列表 + 无 shell，且来自本机配置
            list(command.argv),
            cwd=str(command.cwd) if command.cwd is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except (OSError, ValueError):
        return None
    return int(process.pid)


def ensure_tts_service(
    *,
    environ: "os._Environ[str] | dict[str, str] | None" = None,
    enabled: bool | None = None,
    timeout_seconds: float = DEFAULT_START_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    reachable: Callable[[], bool] | None = None,
    spawn: Callable[[TtsStartCommand], int | None] | None = None,
    command: TtsStartCommand | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TtsStartResult:
    """确保本机 TTS 服务可用：已在跑就直接返回，否则按配置拉起并等待就绪。

    ``reachable`` / ``spawn`` / ``clock`` / ``sleep`` 全部可注入，离线测试不会真的
    启进程或发网络请求。任何失败都只体现在返回值的 ``reachable``/``error`` 上，
    **不抛异常**（由调用方决定降级话术）。
    """

    probe = reachable if reachable is not None else _http_reachable
    launcher = spawn if spawn is not None else _spawn
    allowed = autostart_enabled(environ) if enabled is None else bool(enabled)
    if not allowed:
        return TtsStartResult(enabled=False, already_reachable=False, started=False,
                              reachable=bool(probe()))
    if probe():
        return TtsStartResult(enabled=True, already_reachable=True, started=False, reachable=True)
    resolved = command if command is not None else build_start_command(environ)
    if resolved is None:
        return TtsStartResult(enabled=True, already_reachable=False, started=False,
                              reachable=False, error="command_not_configured")
    started_at = clock()
    pid = launcher(resolved)
    if pid is None:
        return TtsStartResult(enabled=True, already_reachable=False, started=False,
                              reachable=False, error="spawn_failed",
                              command=resolved.argv)
    deadline = started_at + max(0.0, float(timeout_seconds))
    interval = max(0.05, float(poll_interval_seconds))
    while True:
        if probe():
            return TtsStartResult(enabled=True, already_reachable=False, started=True,
                                  reachable=True, pid=pid,
                                  elapsed_seconds=clock() - started_at,
                                  command=resolved.argv)
        if clock() >= deadline:
            return TtsStartResult(enabled=True, already_reachable=False, started=True,
                                  reachable=False, pid=pid,
                                  elapsed_seconds=clock() - started_at,
                                  error="start_timeout", command=resolved.argv)
        sleep(interval)


class OwnedProcess:
    """只记录"本进程拉起的那个 pid"，用于收尾时按需关闭。"""

    def __init__(self) -> None:
        self._pid: int | None = None

    @property
    def pid(self) -> int | None:
        return self._pid

    def remember(self, result: TtsStartResult) -> None:
        if result.enabled and result.started and result.pid is not None:
            self._pid = int(result.pid)

    def shutdown_owned(self) -> bool:
        """只关掉自己拉起的进程；已在你机器上跑着的服务绝不动。返回是否真的关了。"""

        pid = self._pid
        if pid is None:
            return False
        self._pid = None
        if os.name != "nt":
            try:
                os.kill(pid, 15)
            except OSError:
                return False
            return True
        try:
            result = subprocess.run(  # noqa: S603,S607 - 固定 argv，无 shell
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, ValueError):
            return False
        return result.returncode == 0


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_START_TIMEOUT_SECONDS",
    "ENV_AUTOSTART",
    "ENV_BACKEND",
    "ENV_CWD",
    "ENV_PYTHON",
    "OwnedProcess",
    "PROBE_TIMEOUT_SECONDS",
    "PROFILE_PROBE_PATH",
    "TTS_BASE_URL",
    "TtsServiceError",
    "TtsStartCommand",
    "TtsStartResult",
    "autostart_enabled",
    "build_start_command",
    "ensure_tts_service",
]
