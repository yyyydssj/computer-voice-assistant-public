# P7-R-18 Provider selection config boundary handoff

- Date: 2026-09-27
- Project: `<PROJECT_ROOT>`
- Status: **provider selection config boundary complete; default runtime remains local / DeepSeek NOT READY**
- Previous handoff: `implementation_plan/checkpoints/2026-09-27-P7-R-17-deepseek-provider-integration-handoff.md`

## Scope authorized for this batch

This batch completed the Provider selection configuration boundary. The supported explicit choices are `local` and `deepseek`, with a stable provider-integration seam for later work. No API key was read, no transport was created, no network request was made, and no ExecutionLedger, Executor, Verification, Audit, TTS, P8, or ASR execution chain was connected.

## Changes made

- `app/config/settings.py`
  - Added `ProviderName = Literal["local", "deepseek"]`.
  - Added `Settings.provider`, defaulting to `local`.
  - Added `ASSISTANT_PROVIDER` provider selection.
  - Invalid provider values fail closed.
  - `public_summary()` projects provider and `provider_ready=false` without exposing secrets.
- `app/config/__init__.py`
  - Exports `ProviderName`.
- `app/providers/provider_integration.py`
  - Added the provider-neutral integration configuration and selection boundary.
  - `local` uses an injected local adapter; DeepSeek readiness remains adapter-controlled.
- `tests/foundation/test_config.py`
  - Covers default local, explicit DeepSeek, invalid provider, and public summary behavior.
- `tests/providers/test_provider_integration.py`
  - Covers the integration configuration and provider selection boundary.

## Verification

- Config tests: **20/20** passed.
- Provider integration tests: **6/6** passed.
- Full unittest discovery: **660/660** passed.
- `compileall app tests`: passed.
- `git diff --check`: passed.
- Execution audit found no HTTP, socket, process launch, secret read, or computer side effect.

## Safety and boundary conclusion

- The default provider remains `local`.
- `app/main.py` was not connected to ProviderIntegration in this batch; `provider_ready` remains `false`.
- `DEEPSEEK_API_KEY` remains outside the credential boundary; public summaries contain no secret value.
- No DeepSeek network request, fallback, or real runtime wiring was implemented.
- No `git add`, commit, or push was performed.

## Next stop point

P7-R-18 completed the provider selection configuration boundary. Any runtime wiring requires separate confirmation of the startup entry point, provider readiness, credential source, network request count, logging redaction, rollback, and side-effect isolation. No runtime connection or network activity is allowed without new authorization.

**DEFAULT RUNTIME INTEGRATION NOT READY / STOP**