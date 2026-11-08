# P7-R-19 Runtime provider integration handoff

- Date: 2026-09-27
- Project: `<PROJECT_ROOT>`
- Status: **runtime provider selection boundary wired; default runtime remains local / DeepSeek NOT READY**
- Previous handoff: `implementation_plan/checkpoints/2026-09-27-P7-R-18-provider-selection-config-boundary-handoff.md`

## Scope authorized for this batch

This batch connected `Settings.provider` to the ProviderIntegration seam in `app/main.py` while preserving the default `local` provider. It established only runtime selection and status projection. It did not use the network, read secret values, create an HTTP transport, connect an Agent, or enter the ExecutionLedger, Executor, Verification, Audit, TTS, P8, ASR, or computer side-effect chains.

## Changes made

- `app/main.py`
  - Added `build_provider_integration(settings)`, mapping `Settings.provider` to `ProviderIntegrationConfig`.
  - Connected the provider integration boundary; `local` uses an injected runtime-local router placeholder and does not start a real provider or Agent.
  - Projects provider and `provider_ready` into `config_checked`, `runtime_ready`, and startup status events.
  - Default local `provider_ready` remains `false` because the local classifier/Agent is not wired.
  - Explicit DeepSeek without an injected adapter remains `provider_ready=false`; no HTTP transport is created, `DEEPSEEK_API_KEY` is not read, and there is no fallback to local.
- `tests/foundation/test_main.py`
  - Covers default local provider integration.
  - Covers explicit DeepSeek remaining not-ready without an adapter.
  - Covers provider/readiness logging and secret non-disclosure.

## Verification

- `test_main.py`: **7/7** passed.
- `test_config.py`: **20/20** passed.
- `test_provider_integration.py`: **6/6** passed.
- Full unittest discovery: **663/663** passed.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: passed.
- Execution audit found no HTTP, socket, process launch, secret read, Agent, tool, TTS, ASR, or computer side-effect call.

## Safety and boundary conclusion

- The default provider remains `local`; the provider-neutral integration boundary is now wired into the startup entry point, but readiness remains `false`.
- Explicit `deepseek` without an adapter remains not-ready; it does not fall back and does not use the network.
- `DEEPSEEK_API_KEY` is not read or logged.
- Agent integration is not present; runtime readiness does not mean Agent or tool execution is available.
- No `git add`, commit, or push was performed.

## Next stop point

P7-R-19 runtime provider-selection wiring is complete. Entering the DeepSeek runtime adapter, Agent invocation, or real network request path requires new authorization with explicit credential source, request count, cost/rate-limit, log-redaction, rollback, and computer-side-effect boundaries.

**NOT READY / STOP: DeepSeek runtime, Agent, and real network requests remain disconnected.**