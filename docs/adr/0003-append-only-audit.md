# ADR 0003 — Append-only audit log, written out of band on failure

**Status:** accepted · 2026-08-29

## Context
Audit rows must survive the events they describe. Denied access and failed logins raise, which
rolls back the request transaction — and would have discarded the very rows that matter most.

## Decision
`audit_logs` is append-only (a PostgreSQL trigger rejects UPDATE and DELETE). Failure-path events
(`auth.login.failed`, `auth.login.locked`, `auth.mfa.failed`, `access.denied`) are written through
`record_out_of_band()`, which commits in its own transaction.

## Consequences
- A rolled-back request still leaves an audit trail.
- Out-of-band writes cannot be undone by the caller, which is the intent; they carry no
  business state, only the event.
