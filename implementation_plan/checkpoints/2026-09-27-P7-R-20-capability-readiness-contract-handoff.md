# P7-R-20 Capability/readiness contract handoff

- Date: 2026-09-27
- Project: `<PROJECT_ROOT>`
- Status: **capability/readiness contract complete; DeepSeek runtime, Agent, transport, and network remain NOT READY**
- Previous handoff: `implementation_plan/checkpoints/2026-09-27-P7-R-19-runtime-provider-integration-handoff.md`

## Scope authorized for this batch

This offline batch formalized the provider capability/readiness status contract and aligned startup status projections with that contract. It did not read `DEEPSEEK_API_KEY`, construct an HTTP transport, make a real HTTP request, initialize an SDK, invoke an Agent, connect ToolRegistry/ExecutionLedger/Executor/Verification/Audit, or enter TTS, ASR, P8, or computer-side-effect paths.

## Changes made

- `app/providers/provider_integration.py`
  - Added immutable `ProviderCapabilitySnapshot` with the non-sensitive fields:
    `provider`, `adapter_injected`, `adapter_ready`, `provider_ready`,
    `runtime_ready`, `agent_ready`, and `transport_ready`.
  - Added `to_dict()` as a fresh, log-safe projection. It contains no transport object,
    credentials, raw payload, or adapter internals.
  - Added `ProviderIntegration.capabilities` and the explicit
    `capability_snapshot` alias.
  - Kept `runtime_ready`, `agent_ready`, and `transport_ready` hard-false for this
    offline boundary.
  - Preserved the readiness distinction: local `provider_ready=True` means the local
    provider seam is selected; it does not mean the runtime or Agent can execute.
    DeepSeek without an injected adapter remains not ready. Injecting the current
    offline adapter does not promote runtime, Agent, or transport readiness.
- `app/providers/__init__.py`
  - Exported `ProviderCapabilitySnapshot`.
- `app/main.py`
  - Uses one capability snapshot projection for `config_checked`, `runtime_ready`,
    and TextShell status, preventing conflicting readiness fields.
  - Startup remains side-effect-free with respect to provider transport and secrets.
- `tests/providers/test_provider_integration.py`
  - Covers local and explicit DeepSeek snapshots, closed DeepSeek-without-adapter
    state, injected-adapter non-promotion, immutable snapshots, and safe projections.
- `tests/foundation/test_main.py`
  - Covers startup projection consistency, local/DeepSeek readiness fields, and
    secret non-disclosure.

## Capability/readiness semantics

| Field | Meaning in P7-R-20 |
|---|---|
| `provider` | Selected provider name (`local` or `deepseek`). |
| `adapter_injected` | An adapter object was explicitly supplied to the integration seam. |
| `adapter_ready` | The injected adapter reports provider-seam readiness. |
| `provider_ready` | The selected provider integration seam is ready; this is not full runtime readiness. |
| `runtime_ready` | End-to-end runtime path is connected; remains `False`. |
| `agent_ready` | Agent path is connected and authorized; remains `False`. |
| `transport_ready` | A real provider transport is connected and authorized; remains `False`. |

## Current states

- **Default/local runtime:**
  `provider=local`, `provider_ready=True`, `runtime_ready=False`,
  `agent_ready=False`, `transport_ready=False`. If an adapter is passed only for
  an offline test, `adapter_injected` may be `True`, but it is never used by the
  local route and does not change the closed runtime state.
- **Explicit DeepSeek without adapter:** all readiness flags are `False` except the
  selected provider name; routing fails closed rather than falling back to local.
- **Explicit DeepSeek with the current offline adapter:** the adapter can be exercised
  only through an explicitly injected fake/offline transport in tests. The status
  contract still reports `runtime_ready=False`, `agent_ready=False`, and
  `transport_ready=False`; this is not evidence of real network capability.

## Verification

- Provider + foundation startup tests: **15/15** passed.
- Full unittest discovery: **665/665** passed.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: passed (existing `app/routing.py` line-ending warning only).
- Execution-surface audit of the P7-R-20 files found no HTTP client, socket, subprocess,
  secret read, Agent invocation, tool/execution/verification/audit call, TTS, ASR, or
  computer-side-effect path. Test-only fake transport injection remains offline.

## Safety and boundary conclusion

- No real provider transport or network request was made.
- No API key value was read or logged.
- No Agent, tool, execution ledger, verification, audit, voice, or computer action was connected.
- Existing unrelated workspace modification `app/routing.py` was preserved.
- No `git add`, commit, or push was performed.

## Next stop point

P7-R-20 is complete. The next stop remains before entering a real DeepSeek runtime,
Agent invocation, or network request. Any future authorization must explicitly define
credential source, request count/cost/rate limits, transport implementation, timeout/
retry policy, log redaction, rollback, and computer-side-effect boundaries.

**NOT READY / STOP: DeepSeek runtime, Agent, transport, and real network requests remain disconnected.**
