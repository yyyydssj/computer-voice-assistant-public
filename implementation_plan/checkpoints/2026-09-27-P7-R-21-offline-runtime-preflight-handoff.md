# P7-R-21 Offline runtime-preflight contract handoff

- Date: 2026-09-27
- Project: `<PROJECT_ROOT>`
- Status: **offline preflight contract implemented and tested; real DeepSeek runtime, Agent, and transport remain NOT READY / STOP**
- Previous handoff: `implementation_plan/checkpoints/2026-09-27-P7-R-20-capability-readiness-contract-handoff.md`

## Authorized scope and changes

The user authorized only an offline runtime-preflight contract after P7-R-20. Changes are limited to `app/providers/provider_integration.py`, `app/providers/__init__.py`, and `tests/providers/test_provider_integration.py`:

- An immutable `DeepSeekRuntimePreflight` lists the declared credential-source **label**, per-batch request limit, cost/rate limits, named HTTPS transport, timeout, retry limit, log-redaction requirement, rollback declaration, and computer-side-effect prohibition. It accepts only the environment-variable source label, never a credential value.
- `ProviderIntegration.assess_runtime_preflight()` reports missing requirement names via immutable `DeepSeekPreflightAssessment`. Complete documentation is **not** execution permission: `runtime_allowed` is always false and cannot be constructed as true.
- The new classes are exported for explicit offline checks. Existing provider routing is unchanged; `runtime_ready`, `agent_ready`, and `transport_ready` stay false. The contract does not prove that any declaration is implemented or authorized.
- Offline tests cover missing and invalid terms, no secret lookup, no network call, immutable results, safe projections, and failed attempts to promote readiness.

## Git baseline and verification

At the start of this batch, `HEAD` tracked only three files. The three P7-R-21 files depended on eleven pre-existing, untracked application files and a pre-existing, modified `app/routing.py`. The user rejected a non-runnable three-file commit and authorized preparation of a separately reviewable dependency baseline. **No previous unrelated files or existing modifications may be silently attributed to P7-R-21.**

- Dependency baseline **prepared for user review, not yet committed or pushed**: `app/__init__.py`, `app/config/{__init__.py,settings.py}`, `app/contracts/{__init__.py,base.py,routing.py,audio.py,execution.py,planning.py,session.py}`, `app/providers/deepseek_adapter.py`, plus existing modifications to `app/routing.py`.
- The baseline's already-tracked `tests/routing/test_provider_schema_fixtures.py` passed **5/5** when run in an isolated temporary checkout populated only from staged baseline blobs and the tracked test. The baseline does **not** claim to include all 671 existing workspace tests.
- P7-R-21 workspace test discovery passed **671/671**; `python -m compileall -q app tests` and `git diff --check` passed (the existing `app/routing.py` line-ending warning is not a diff-check failure).
- No key value was read, no HTTP request was made, no Agent/tool execution was connected, and no computer side effect was performed. No commit or push had been performed at handoff preparation time.

## Next batch entry: P7-R-22 (requires separate authorization)

**Entry point:** only after review and acceptance of the dependency baseline and P7-R-21 commit, propose P7-R-22 as an **offline configuration-to-preflight consistency audit**. Check that declared transport name, timeout, retry count, request budget, and status projections match the already present adapter/config contracts using fake inputs; first present the exact files, acceptance tests, and unchanged execution boundary for approval. A documentation-complete assessment is not consent to send a request.

**Stop conditions:** do not read `DEEPSEEK_API_KEY`, construct/connect a real HTTP transport, call an Agent, make a network request, change default provider, or enter tools/ExecutionLedger/Executor/Verification/Audit/TTS/ASR/P8/computer-side-effect paths. Any later real-provider batch needs separate explicit credential-source, target, request-count, cost/rate, timeout/retry, redaction, rollback, and side-effect authorizations. Until then, **NOT READY / STOP**.