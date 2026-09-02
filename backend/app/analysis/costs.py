"""Per-stage cost/token recording, summed onto the analysis row (P5-T5).

A real OCR/LLM stage function (still blocked - see `app.analysis.stages`'s
module docstring) calls `record_stage_cost` itself, using the same `db`/
`analysis` `advance_analysis` already hands it, once its provider call
returns real usage numbers. Nothing here assumes who called it or how many
times - repeated calls across a run simply keep summing, which is the whole
point: the analysis's own totals are always "cost so far," correct whether
read mid-run or after `completed`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.analysis.models import Analysis


def record_stage_cost(
    db: Session,
    analysis: Analysis,
    *,
    stage: str,
    provider: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_cents: int = 0,
) -> Analysis:
    """`stage`/`provider` are accepted (and will matter once a real per-stage
    cost breakdown is wanted) but not separately persisted yet - only the
    running totals are, matching IMPLEMENTATION.md §14/§28's "on the analysis
    row" wording. Callers should still pass them for forward compatibility
    and so a structured log line can name them."""
    analysis.total_tokens_in += tokens_in
    analysis.total_tokens_out += tokens_out
    analysis.total_cost_cents += cost_cents
    db.add(analysis)
    db.flush()
    return analysis
