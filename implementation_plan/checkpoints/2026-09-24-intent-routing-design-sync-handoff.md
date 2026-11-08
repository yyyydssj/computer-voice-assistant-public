# Intent-routing contract design sync handoff

- Date: 2026-09-24
- Stage: P7-R (intent-routing contract and ConversationManager seam design)
- Status: **design sync complete; code implementation not_started**
- Project: `<PROJECT_ROOT>`

## Sole objective

Integrate the independent intent-routing contract into the existing assistant design so TextShell and ASR accepted final use one business seam, with no second session, authorization, or execution chain.

## Unified chain

```text
TextShell / ASR accepted final
-> ConversationManager
-> IntentRouter
-> RouteHandoff
-> AgentAdapter / read-only tools / ActionPlan / Safety
-> authorization
-> Execution Ledger
-> Executor
-> Verification
-> Audit
```

## Frozen contract facts

1. Two-level structure: `route + intent`.
2. High-level routes: `chat`, `query`, `task`, `dangerous`, `ambiguous`, `unsupported`.
3. Every handoff requires `request_id`, `conversation_id`, `turn_id`, `source`, `raw_text`, and `created_at`.
4. `conversation_id` reuses `Conversation.session_id`; `source` is `text_shell | asr`; existing `Utterance.input_mode` remains.
5. Normal classification, low confidence, timeout, unavailable provider, invalid output, and inconsistent routing all return a `RouteHandoff`; failure normalizes to `route=ambiguous`.
6. The Agent cannot freely reclassify after routing failure.
7. Slots are candidate parameters; classifier output must use `confirmed=false`; only `ConversationManager` may set true after explicit confirmation and an allowed safety transition.
8. The router cannot authorize, execute, call computer tools, create `AuthorizationSnapshot`, write `ExecutionLedger`, or lower risk.
9. The current Laya `billing/technical` model cannot be directly mapped to assistant intents; use a contract-compatible model or strict adapter and validate separately.

## Explicitly not done

- No changes to `app/` or `tests/`.
- No real Laya connection, service start, or computer side effect.
- No `IntentRouter`, `RouteHandoff` schema, or provider adapter implementation.
- No P7/TTS code or P8 voice-loop implementation.

## Next implementation entry

```text
RouteHandoff schema/version
-> pure IntentRouter interface
-> failure normalizer
-> ConversationManager(text_shell + asr accepted final) integration
-> provider adapter and tests
-> evaluate a contract-compatible Laya intent model
```

Implementation must retain:

```text
Plan -> preflight -> authorization -> ledger -> executor -> verification -> audit
```
