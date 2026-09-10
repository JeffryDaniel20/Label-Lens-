# Runbook — LLM/OCR provider outage

Applies when the configured extraction provider (`LABELLENS_LLM_PROVIDER`, currently `gemini` per
D-06) or the cloud OCR fallback (P3-T3, blocked on D-03) is down, rate-limited, or returning
persistent errors.

## Symptoms

- `extracting` stage jobs dead-lettering with `PermanentStageError`/`TransientStageError` from
  `app.extraction.llm.gemini` (check `dead_letter_jobs.error_message` via
  `GET /v1/analyses/dead-letters`).
- A spike in analyses stuck in `extracting` past `retry_policy.ANALYSIS_BUDGET_SECONDS` (20 min),
  reaped by `app.analysis.janitor` as `stalled`.
- Elevated 5xx/429 rates in structured logs for the `extracting` stage specifically (check the
  `stage` field structlog always carries - section 20).

## Immediate response

1. Confirm it's the provider, not this codebase: check the provider's own status page, and try one
   manual call with the configured `LABELLENS_LLM_API_KEY` outside the app (curl/SDK) to rule out a
   local credential or network issue first.
2. If genuinely a provider outage: no code change fixes this. `app.analysis.retry_policy`'s existing
   backoff+DLQ already keeps failed analyses from silently disappearing - they land in the DLQ as
   `retryable=True` and can be replayed once the provider recovers
   (`POST /v1/analyses/dead-letters/{id}/replay`).
3. If the org has a `LABELLENS_LLM_ESCALATION_MODEL` configured on a *different* provider tier that
   happens to still be healthy, nothing needs to change - escalation already happens automatically
   on schema-validation failure, though not currently on a transport-level outage of the primary
   model specifically (a real gap worth a future task, not something to improvise around mid-incident).

## Mitigation options, in order of how invasive they are

1. **Wait and replay** - if the outage is short, let affected analyses dead-letter and replay them
   afterward. No configuration change needed.
2. **Switch provider entirely** - `LABELLENS_LLM_PROVIDER`/`LABELLENS_LLM_API_KEY`/
   `LABELLENS_LLM_MODEL` are plain env vars (`app/extraction/llm/__init__.py::build_provider`
   dispatches on `LABELLENS_LLM_PROVIDER`); redeploying with a different provider set is a
   configuration change, not a code change, by design (D-06's own rationale).
3. **Disable cloud extraction for the affected org(s)** - `organizations.cloud_ai_enabled` exists for
   exactly this (section 33's risk table: "disable cloud extraction per-org"); flipping it routes
   that org to local-only mode once local extraction exists as a real path (not yet built - today
   this would mean pausing new analyses for that org instead).

## After the outage

- Replay every dead-lettered analysis that was `retryable=True` and dead-lettered for this reason.
- Note the outage window and affected analysis count in the incident log (section 20's audit trail
  already records `analysis lifecycle` events with timestamps - no separate log needed for the raw
  data, just a written summary of what happened and why).
