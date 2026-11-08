r"""R 支线：真实 DeepSeek Provider 有界探测 runner（需 ``--live-opt-in``）。

本 runner 只做一件事：在用户逐项授权范围内，对真实 Provider 发起**有界、脱敏、隔离**的
路由探测，判断真实输出能否通过既有 ``deepseek-route/v1`` 严格解析，并如实记录失败行为。

授权边界（本轮由用户逐项确认）：

- 目标：仅最小真实探测，**不**接入默认运行时、**不**解除 adapter 的 NOT READY；
- 模型：``deepseek-flash``（官方当前名；legacy ``deepseek-v4-flash`` 对应模型已退役）；
- 凭据：仅从当前进程环境读 ``DEEPSEEK_API_KEY``；不打印、不落盘、不写入 ``.env``；
- 网络：仅 ``https://api.deepseek.com/chat/completions``，HTTPS，无其他域名或端口；
- 预算：总请求 ≤3 次、无重试、单次超时 ≤15 秒、``max_tokens=256``、费用上限 ≤0.05 元；
- 日志：请求文本与响应正文都不落盘，只记长度、SHA-256、枚举化状态与耗时；
- 隔离：不创建计划/授权/账本/执行事实，不调用工具，不产生电脑副作用，不改 ``provider_ready``；
- 回滚：删除本文件即可；``DeepSeekAdapterConfig.enabled`` 默认仍为 ``False``，离线路径不变。

用法：

```powershell
# 离线预检（不联网、不发请求、不读密钥值）
.\.venv\Scripts\python.exe tools\r_real_provider_probe.py --check-only

# 真实探测（≤3 次；先在当前进程注入用户级凭据，不落盘）
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
.\.venv\Scripts\python.exe tools\r_real_provider_probe.py --live-opt-in
```
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.contracts import ContractError, RouteTrace  # noqa: E402
from app.providers.deepseek_adapter import (  # noqa: E402
    DeepSeekAdapterConfig,
    DeepSeekProviderAdapter,
    DeepSeekRequest,
)
from app.providers.deepseek_http_transport import DeepSeekHTTPTransport  # noqa: E402
from app.routing import RouteInput  # noqa: E402

ARTIFACT_ROOT = _REPO / "implementation_plan" / "checkpoints" / "artifacts"
API_KEY_ENV = "DEEPSEEK_API_KEY"
ENDPOINT_HOST = "api.deepseek.com"
DEFAULT_MODEL_ID = "deepseek-flash"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_TOKENS = 256
MAX_REQUESTS_LIMIT = 3
TIMEOUT_LIMIT = 15.0
MAX_TOKENS_LIMIT = 4096
COST_CAP_CNY_LIMIT = 0.05

# 2026-09-30 官方文档核实的 Flash 价格（USD / 1M tokens，取高峰价作上界）
_USD_PER_1M_INPUT = 0.30
_USD_PER_1M_OUTPUT = 1.20
_USD_TO_CNY = 7.2

EXIT_OK = 0
EXIT_PROBE_FAILED = 1
EXIT_NOT_OPTED_IN = 2
EXIT_PREFLIGHT_FAILED = 3

SAMPLES: tuple[tuple[str, str], ...] = (
    ("chat-1", "你好，今天天气怎么样？"),
    ("task-1", "帮我打开计算器"),
    ("ambiguous-1", "那个东西帮我弄一下"),
)

_PASSING_STATUSES = frozenset({"success", "low_confidence"})


class ProbeError(RuntimeError):
    """探测前置条件不满足（fail closed，不发起请求）。"""


class FingerprintTransport:
    """包装真实 transport：只记录长度、SHA-256 与 payload 字段名，绝不保留原文。

    响应正文在 ``send`` 返回后立即交给既有严格解析器；本类只保留可审计的非敏感指纹。
    """

    def __init__(self, inner: object, *, diagnostics: dict[str, object] | None = None) -> None:
        self._inner = inner
        self.calls = 0
        self.last: dict[str, object] | None = None
        self.diagnostics: dict[str, object] = diagnostics if diagnostics is not None else {}

    def send(self, request: DeepSeekRequest, *, timeout_seconds: float) -> str:
        payload = request.to_payload()
        base: dict[str, object] = {
            "payload_fields": sorted(payload),
            "request_chars": len(request.raw_text),
        }
        try:
            raw = self._inner.send(request, timeout_seconds=timeout_seconds)  # type: ignore[attr-defined]
        except BaseException as exc:
            self.calls += 1
            self.last = dict(base, error_type=type(exc).__name__)
            raise
        self.calls += 1
        self.last = dict(
            base,
            response_chars=len(raw),
            response_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )
        return raw


CONTRACT_FIELDS = (
    "schema_version",
    "status",
    "route",
    "intent",
    "handling_mode",
    "confidence",
    "slots",
    "required_confirmation",
    "tool_scope",
)
_ENUM_LITERAL_MAX = 32


def _safe_literal(value: object) -> object:
    """只回传短枚举字面值；任何自由文本一律折叠为 ``"<non-enum>"``，避免泄露正文。"""

    if not isinstance(value, str) or len(value) > _ENUM_LITERAL_MAX:
        return "<non-enum>"
    if not value or any(not (char.isalnum() or char in "_-/.") for char in value):
        return "<non-enum>"
    return value


def _first_char_class(text: str) -> str:
    if not text:
        return "empty"
    head = text[0]
    if head == "{":
        return "brace"
    if head == "[":
        return "bracket"
    if head == "`":
        return "backtick"
    if head in "\"'":
        return "quote"
    if head.isalpha():
        return "letter"
    return "other"


def describe_content_shape(content: str) -> dict[str, object]:
    """描述模型 ``content`` 的结构形状：是否裸 JSON、字段名、缺失契约字段、安全枚举值。"""

    stripped = content.strip()
    shape: dict[str, object] = {
        "content_chars": len(content),
        "starts_with_brace": stripped.startswith("{"),
        "ends_with_brace": stripped.endswith("}"),
        "has_markdown_fence": "```" in content,
        "line_count": content.count("\n") + 1,
    }
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        shape["content_json_valid"] = False
        shape["first_char_class"] = _first_char_class(stripped)
        return shape
    shape["content_json_valid"] = True
    if not isinstance(parsed, dict):
        shape["content_json_is_object"] = False
        return shape
    shape["content_json_is_object"] = True
    shape["content_top_keys"] = sorted(parsed)
    shape["content_field_types"] = {name: type(value).__name__ for name, value in sorted(parsed.items())}
    shape["missing_contract_fields"] = [name for name in CONTRACT_FIELDS if name not in parsed]
    for name in ("schema_version", "status", "route", "intent", "handling_mode", "required_confirmation"):
        if name in parsed:
            shape[f"{name}_literal"] = _safe_literal(parsed[name])
    return shape


def describe_body(data: object) -> dict[str, object]:
    """从响应字节中提取**结构指纹**：字段名、长度、枚举值，绝不保留任何正文内容。"""

    if not isinstance(data, (bytes, bytearray)):
        return {"body_readable": False}
    raw = bytes(data)
    described: dict[str, object] = {
        "body_chars": len(raw),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
    }
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        described["json_valid"] = False
        return described
    described["json_valid"] = True
    if not isinstance(envelope, dict):
        described["envelope_is_object"] = False
        return described
    described["envelope_is_object"] = True
    described["envelope_keys"] = sorted(envelope)
    error = envelope.get("error")
    if isinstance(error, dict):
        described["error_keys"] = sorted(error)
        described["error_code_present"] = bool(error.get("code"))
    choices = envelope.get("choices")
    if isinstance(choices, list):
        described["choices_count"] = len(choices)
        if choices and isinstance(choices[0], dict):
            first = choices[0]
            described["choice_keys"] = sorted(first)
            described["finish_reason"] = first.get("finish_reason")
            message = first.get("message")
            if isinstance(message, dict):
                described["message_keys"] = sorted(message)
                content = message.get("content")
                described["content_chars"] = len(content) if isinstance(content, str) else None
                if isinstance(content, str):
                    described["content_shape"] = describe_content_shape(content)
                reasoning = message.get("reasoning_content")
                described["reasoning_content_chars"] = (
                    len(reasoning) if isinstance(reasoning, str) else None
                )
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        described["usage_keys"] = sorted(usage)
    return described


class DiagnosingResponse:
    """包装 HTTP 响应：把结构指纹交给诊断字典，正文仍原样回传给产品 transport。"""

    def __init__(self, inner: object, diagnostics: dict[str, object]) -> None:
        self._inner = inner
        self._diagnostics = diagnostics

    @property
    def status(self) -> object:
        return getattr(self._inner, "status", None)

    def read(self, amount: int = -1) -> bytes:
        data = self._inner.read(amount)  # type: ignore[attr-defined]
        self._diagnostics.update(describe_body(data))
        return data

    def close(self) -> None:
        self._inner.close()  # type: ignore[attr-defined]


def make_diagnosing_opener(diagnostics: dict[str, object]):
    """构造诊断 opener：只记录 HTTP 状态码与传输错误类型，不读也不打印正文。"""

    from urllib.error import HTTPError, URLError
    from urllib.request import urlopen

    def opener(request: object, *, timeout: float) -> object:
        try:
            response = urlopen(request, timeout=timeout)  # type: ignore[arg-type]
        except HTTPError as exc:
            diagnostics["http_status"] = exc.code
            raise
        except URLError as exc:
            diagnostics["transport_error_type"] = type(exc.reason).__name__ if exc.reason else "URLError"
            raise
        diagnostics["http_status"] = getattr(response, "status", None)
        return DiagnosingResponse(response, diagnostics)

    return opener


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_config(args: argparse.Namespace) -> DeepSeekAdapterConfig:
    return DeepSeekAdapterConfig(
        provider_name="deepseek-official",
        model_id=args.model_id,
        model_version="probe-live-2026-09-30",
        base_url="https://api.deepseek.com",
        enabled=True,
        timeout_seconds=args.timeout_seconds,
        max_retries=0,
        max_tokens=args.max_tokens,
        thinking=None if args.thinking == "default" else args.thinking,
    )


def credential_present() -> bool:
    """只判断凭据是否存在，绝不返回或打印其值。"""

    value = os.environ.get(API_KEY_ENV)
    return type(value) is str and bool(value.strip())


def select_samples(args: argparse.Namespace) -> tuple[tuple[str, str], ...]:
    """按 ``--sample-id`` 选择样例；未指定时使用全部样例。"""

    if not args.sample_id:
        return SAMPLES
    wanted = list(dict.fromkeys(args.sample_id))
    return tuple(item for item in SAMPLES if item[0] in wanted)


def estimate_cost_upper_cny(
    request_count: int,
    samples: tuple[tuple[str, str], ...] = SAMPLES,
) -> float:
    """按输入字符数上界（保守 1 char ≈ 1 token）与 max_tokens 估算费用上界。"""

    input_tokens = sum(len(text) for _, text in samples[:request_count])
    output_tokens = request_count * DEFAULT_MAX_TOKENS
    usd = (
        input_tokens / 1_000_000 * _USD_PER_1M_INPUT
        + output_tokens / 1_000_000 * _USD_PER_1M_OUTPUT
    )
    return usd * _USD_TO_CNY


def build_authorization(args: argparse.Namespace) -> dict[str, object]:
    return {
        "scope": "minimal_live_probe_only",
        "runtime_wiring": False,
        "provider_ready_unchanged": True,
        "model_id": args.model_id,
        "credential_source": f"process environment {API_KEY_ENV}",
        "credential_value_logged": False,
        "credential_written_to_disk": False,
        "network_target": "https://api.deepseek.com/chat/completions",
        "allowed_hosts": ["api.deepseek.com"],
        "max_requests": args.max_requests,
        "samples": [sample_id for sample_id, _ in select_samples(args)],
        "max_retries": 0,
        "timeout_seconds": args.timeout_seconds,
        "max_tokens": args.max_tokens,
        "thinking": args.thinking,
        "diagnostic_mode": bool(args.diagnose),
        "cost_cap_cny": args.cost_cap_cny,
        "log_redaction": "lengths+sha256+enumerated status only",
        "payload_isolation": True,
        "computer_side_effects": False,
    }


def build_boundaries() -> dict[str, object]:
    return {
        "default_runtime_touched": False,
        "conversation_manager_touched": False,
        "tool_registry_touched": False,
        "execution_ledger_touched": False,
        "executor_touched": False,
        "verification_touched": False,
        "audit_touched": False,
        "plan_authorization_created": False,
        "raw_request_text_persisted": False,
        "raw_response_body_persisted": False,
        "credentials_persisted": False,
    }


def preflight(args: argparse.Namespace) -> dict[str, object]:
    """离线预检：构造配置与请求对象、核对预算与凭据存在性，不联网。"""

    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "required": required})

    try:
        config = build_config(args)
        add("config_valid", True, f"host={ENDPOINT_HOST} enabled={config.enabled}")
    except ContractError as exc:
        add("config_valid", False, type(exc).__name__)
        raise ProbeError("配置构造失败，已终止") from None

    add(
        "host_allowlisted",
        config.base_url.startswith("https://") and ENDPOINT_HOST in config.allowed_hosts,
        ",".join(config.allowed_hosts),
    )
    add("timeout_bounded", config.timeout <= TIMEOUT_LIMIT, f"{config.timeout}s <= {TIMEOUT_LIMIT}s")
    add("no_retry", config.max_retries == 0, f"max_retries={config.max_retries}")

    payload_keys: list[str] = []
    try:
        sample_request = DeepSeekRequest(
            raw_text=SAMPLES[0][1],
            model_id=config.model_id,
            messages=(("system", "deepseek-route/v1"), ("user", SAMPLES[0][1])),
            max_tokens=config.max_tokens,
        )
        payload_keys = sorted(sample_request.to_payload())
        add("max_tokens_sent", "max_tokens" in payload_keys, ",".join(payload_keys))
    except ContractError as exc:
        add("max_tokens_sent", False, type(exc).__name__)

    try:
        thinking_request = DeepSeekRequest(
            raw_text=SAMPLES[0][1],
            model_id=config.model_id,
            messages=(("system", "deepseek-route/v1"), ("user", SAMPLES[0][1])),
            thinking=config.thinking,
        )
        thinking_payload = thinking_request.to_payload()
        add(
            "thinking_field",
            args.thinking == "default" or thinking_payload.get("thinking") == {"type": args.thinking},
            f"mode={args.thinking} payload={thinking_payload.get('thinking', 'absent')}",
        )
    except ContractError as exc:
        add("thinking_field", False, type(exc).__name__)

    legacy_payload_keys: list[str] = []
    legacy_request = DeepSeekRequest(
        raw_text=SAMPLES[0][1],
        model_id=config.model_id,
        messages=(("system", "deepseek-route/v1"), ("user", SAMPLES[0][1])),
    )
    legacy_payload_keys = sorted(legacy_request.to_payload())
    add(
        "legacy_payload_unchanged",
        "max_tokens" not in legacy_payload_keys,
        ",".join(legacy_payload_keys),
    )

    estimated = estimate_cost_upper_cny(args.max_requests, select_samples(args))
    add(
        "cost_within_cap",
        estimated <= args.cost_cap_cny,
        f"estimated_upper={estimated:.6f} CNY <= cap {args.cost_cap_cny} CNY",
    )
    credential_available = credential_present()
    add(
        "credential_present",
        credential_available,
        f"{API_KEY_ENV} available={credential_available} (value never logged)",
        required=False,
    )

    return {
        "checks": checks,
        "passed": all(bool(item["ok"]) for item in checks if item["required"]),
        "payload_keys": payload_keys,
        "estimated_upper_cny": round(estimated, 6),
    }


def run_live_probe(args: argparse.Namespace) -> list[dict[str, object]]:
    config = build_config(args)
    diagnostics: dict[str, object] = {}
    opener = make_diagnosing_opener(diagnostics) if args.diagnose else None
    transport = FingerprintTransport(
        DeepSeekHTTPTransport(config=config, opener=opener),
        diagnostics=diagnostics,
    )
    adapter = DeepSeekProviderAdapter(transport, config=config)

    rounds: list[dict[str, object]] = []
    for index, (sample_id, text) in enumerate(select_samples(args)[: args.max_requests], start=1):
        trace = RouteTrace(
            request_id=f"r-probe-request-{index}",
            conversation_id="r-probe-conversation",
            turn_id=f"r-probe-turn-{index}",
            source="text_shell",
            raw_text=text,
            created_at=utc_iso(),
        )
        record: dict[str, object] = {
            "index": index,
            "sample_id": sample_id,
            "input_chars": len(text),
        }
        started = time.perf_counter()
        try:
            handoff = adapter.route(RouteInput(raw_text=text, trace=trace))
        except BaseException as exc:  # 只记录异常类型，绝不记录异常正文
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            record["error_type"] = type(exc).__name__
            record["strict_parse_passed"] = False
            if diagnostics:
                record["response_structure"] = dict(sorted(diagnostics.items()))
                diagnostics.clear()
            if transport.last is not None:
                record.update(transport.last)
            rounds.append(record)
            break
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record["router_status"] = handoff.router.status
        record["failure_code"] = handoff.router.failure_code
        record["route"] = handoff.route
        record["intent"] = handoff.intent
        record["handling_mode"] = handoff.handling_mode
        record["confidence"] = handoff.confidence
        record["slot_count"] = len(handoff.slots)
        record["tool_scope"] = list(handoff.tool_scope)
        record["required_confirmation"] = handoff.required_confirmation
        record["strict_parse_passed"] = handoff.router.status != "invalid_response"
        if transport.last is not None:
            record.update(transport.last)
            transport.last = None
        if diagnostics:
            record["response_structure"] = dict(sorted(diagnostics.items()))
            diagnostics.clear()
        rounds.append(record)
        if handoff.router.status not in _PASSING_STATUSES:
            break
    return rounds


def build_verdict(rounds: list[dict[str, object]]) -> dict[str, object]:
    passed = [item for item in rounds if item.get("strict_parse_passed")]
    routes = [item.get("route") for item in rounds if item.get("route")]
    return {
        "requests_attempted": len(rounds),
        "strict_parse_passed_count": len(passed),
        "routes_observed": routes,
        "failure_statuses": [
            item.get("router_status", item.get("error_type")) for item in rounds if not item.get("strict_parse_passed")
        ],
        "all_strict_parse_passed": bool(rounds) and len(passed) == len(rounds),
        "provider_ready": False,
        "runtime_wiring_authorized": False,
        "evidence_limits": [
            "既有 transport 有意不暴露 HTTP 状态码与响应正文；其 ValueError 同时覆盖 4xx 拒绝、"
            "非 JSON 正文与不满足 deepseek-route/v1 的结构，本轮无法据此进一步区分。",
            "响应正文只在内存中经过严格解析器，artifact 仅保留长度、SHA-256 与枚举化状态。",
        ],
        "note": "本轮只证明真实调用链与严格解析表现；未解除 NOT READY，未接入默认运行时。",
    }


def print_rounds(rounds: list[dict[str, object]]) -> None:
    for item in rounds:
        status = item.get("router_status", item.get("error_type"))
        print(
            f"  #{item['index']} {item['sample_id']}: status={status} "
            f"route={item.get('route', '-')} intent={item.get('intent', '-')} "
            f"confidence={item.get('confidence', '-')} elapsed_ms={item.get('elapsed_ms', '-')} "
            f"strict={item.get('strict_parse_passed')}"
        )


def write_artifact(payload: dict[str, object], mode: str) -> Path:
    directory = ARTIFACT_ROOT / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-R-real-provider-probe"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    path = directory / f"r-real-provider-probe-{utc_stamp()}-{mode}-{digest}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="R 支线真实 DeepSeek Provider 有界探测 runner")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-only", action="store_true", help="离线预检：不联网、不发请求")
    mode.add_argument("--live-opt-in", action="store_true", help="显式授权真实探测（≤3 次请求）")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--max-requests", type=int, default=MAX_REQUESTS_LIMIT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--cost-cap-cny", type=float, default=COST_CAP_CNY_LIMIT)
    parser.add_argument(
        "--thinking",
        choices=("disabled", "enabled", "default"),
        default="disabled",
        help="请求体 thinking 字段；default 表示不发送（服务端默认开启 thinking, effort=high）",
    )
    parser.add_argument(
        "--sample-id",
        action="append",
        choices=[sample_id for sample_id, _ in SAMPLES],
        default=None,
        help="只跑指定样例（可重复）；默认跑全部样例",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="额外记录 HTTP 状态码与响应结构指纹（字段名/长度/枚举，不含任何正文）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not 1 <= args.max_requests <= MAX_REQUESTS_LIMIT:
        print(f"max-requests 必须在 1..{MAX_REQUESTS_LIMIT}")
        return EXIT_NOT_OPTED_IN
    if not 0 < args.timeout_seconds <= TIMEOUT_LIMIT:
        print(f"timeout-seconds 必须在 0..{TIMEOUT_LIMIT}")
        return EXIT_NOT_OPTED_IN
    if not 1 <= args.max_tokens <= MAX_TOKENS_LIMIT:
        print(f"max-tokens 必须在 1..{MAX_TOKENS_LIMIT}")
        return EXIT_NOT_OPTED_IN
    if not 0 < args.cost_cap_cny <= COST_CAP_CNY_LIMIT:
        print(f"cost-cap-cny 必须在 0..{COST_CAP_CNY_LIMIT}")
        return EXIT_NOT_OPTED_IN

    mode = "live" if args.live_opt_in else "check-only"
    payload: dict[str, object] = {
        "schema": "r-real-provider-probe/v1",
        "created_at": utc_iso(),
        "mode": mode,
        "diagnostic_mode": bool(args.diagnose),
        "authorization": build_authorization(args),
        "boundaries": build_boundaries(),
    }

    try:
        preflight_result = preflight(args)
    except ProbeError as exc:
        print(f"预检失败：{exc}")
        return EXIT_PREFLIGHT_FAILED
    payload["preflight"] = preflight_result
    for check in preflight_result["checks"]:
        label = "OK  " if check["ok"] else ("FAIL" if check["required"] else "INFO")
        print(f"[{label}] {check['check']}: {check['detail']}")
    if not preflight_result["passed"]:
        print("预检未全部通过，已终止（未发起任何请求）")
        write_artifact(payload, mode)
        return EXIT_PREFLIGHT_FAILED

    if mode == "check-only":
        payload["rounds"] = []
        payload["verdict"] = {
            "requests_attempted": 0,
            "note": "离线预检模式，未发起真实请求。",
            "provider_ready": False,
        }
        path = write_artifact(payload, mode)
        print(f"离线预检完成，未联网；artifact: {path}")
        return EXIT_OK

    if not credential_present():
        print(f"凭据 {API_KEY_ENV} 在当前进程不可用，fail closed，未发起请求")
        payload["rounds"] = []
        payload["verdict"] = {"requests_attempted": 0, "note": "凭据缺失，fail closed。"}
        write_artifact(payload, mode)
        return EXIT_PREFLIGHT_FAILED

    print(f"开始有界真实探测：model={args.model_id} max_requests={args.max_requests} "
          f"timeout={args.timeout_seconds}s max_tokens={args.max_tokens}")
    rounds = run_live_probe(args)
    payload["rounds"] = rounds
    payload["verdict"] = build_verdict(rounds)
    path = write_artifact(payload, mode)

    print_rounds(rounds)
    print(f"真实探测结束；artifact: {path}")
    return EXIT_OK if payload["verdict"]["all_strict_parse_passed"] else EXIT_PROBE_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
