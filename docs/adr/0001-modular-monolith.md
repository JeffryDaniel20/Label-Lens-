# ADR 0001 — Modular monolith, not microservices

**Status:** accepted · 2026-08-29

## Context
LabelLens is built and operated by one developer. It needs strict module boundaries (identity,
catalog, vision, extraction, rules, analysis, evidence, review, reporting, audit) but cannot afford
the operational surface of independent services.

## Decision
One FastAPI codebase deployed as two processes (API and worker) with enforced internal module
boundaries. `rules` is a pure module: no database, no network, no ambient clock.

## Consequences
- One deploy, one log stream, one database, one backup — operable by one person.
- Boundaries are enforced by import discipline and tests rather than by the network, so a future
  extraction into a service is mechanical.
- Horizontal scale is limited to bigger machines and a separate worker host. Accepted: the
  projected load (thousands of analyses per month) is far below that ceiling.
