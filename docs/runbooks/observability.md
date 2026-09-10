# Runbook — observability (P7-T5)

## What exists

- **Metrics**: `GET /metrics` (no auth, matching `/healthz`/`/readyz`'s posture) exposes Prometheus
  exposition format. Counters/histograms are incremented at the point each event actually happens
  (`app.analysis.state_machine.transition`, `app.analysis.dlq.record_dead_letter`,
  `app.analysis.worker.run_analysis_stage`); the two "current state" gauges
  (`labellens_analyses_by_state`, `labellens_dlq_unreplayed`) and `labellens_queue_depth` are
  computed from a live query/Redis read at scrape time instead, so they can never drift from the
  truth the way a hand-maintained running total could.
- **Alerts**: `infra/prometheus/alerts.yml` implements IMPLEMENTATION.md section 20's three named
  alerts verbatim - DLQ non-empty, analysis failure rate > 5%/15m, queue depth > 50 for 10+ minutes.
- **Dashboards**: `infra/grafana/dashboards/labellens-overview.json`, auto-provisioned.
- **Errors**: Sentry (`LABELLENS_SENTRY_DSN`), capturing every genuinely unhandled exception at the
  HTTP boundary (`app.platform.errors`'s `_unhandled` handler) tagged with the same
  correlation id / org id / actor id every structured log line already carries, so a Sentry issue
  and its corresponding log lines are always cross-referenceable. Disabled (a safe no-op) with no
  DSN configured - the local/test default.
- **Logs**: already structured JSON via structlog (P0-T3) - shipping them to Loki is an ingestion
  concern (promtail/Docker log driver reading stdout), not an application change, and isn't wired up
  in this local dev overlay.

## Running it locally

```
docker compose -f infra/docker-compose.yml -f infra/docker-compose.observability.yml up
```
Prometheus: http://localhost:9090 - Grafana: http://localhost:3001 (anonymous viewer access enabled
for local dev; set real auth before ever exposing this beyond localhost).

Not part of `make up`/the base stack on purpose - this overlay can be brought up any time after
P0-T2 (see IMPLEMENTATION.md's own scheduling note) without burdening a plain local dev boot that
has nothing to do with observability.

## What each alert means and what to do

| Alert | Meaning | First step |
|---|---|---|
| `DeadLetterQueueNonEmpty` | At least one analysis dead-lettered and hasn't been replayed. | `GET /v1/analyses/dead-letters`; if the reason looks provider-related, see [provider-outage.md](provider-outage.md). |
| `AnalysisFailureRateHigh` | Over 5% of analyses failed in the last 15 minutes. | Check `labellens_analysis_failures_total` broken down by `stage` in Grafana/Prometheus to find which pipeline step is failing before assuming a provider outage. |
| `QueueDepthHigh` | A queue (`default`/`ocr`/`llm`) has stayed above 50 pending jobs for 10+ minutes. | Check whether a worker process is actually running and consuming that queue before scaling it - a depth spike from a dead worker looks identical to one from real load. |

## A known verification gap, recorded honestly

The alert rules and dashboard were validated for syntax (valid YAML/JSON) and for referencing real,
correctly-spelled metric names the application actually emits (`tests/integration/
test_observability_config.py`), but were **not** verified against a live Prometheus/Grafana
evaluating a real forced failure end-to-end - a persistent Docker Hub image-pull failure (large
layers repeatedly truncated with EOF across more than a dozen retries and two different image tags,
confirmed to be a local network/CDN issue rather than anything about this project's own config) made
pulling either image impossible in the session that built this. See TESTTEST.md's P7-T5 row for the
full account. Re-running the acceptance drill once images can actually be pulled - force a DLQ
arrival, confirm `DeadLetterQueueNonEmpty` transitions to firing in Prometheus's own UI within one
scrape interval - is the one piece of this task still owed.
