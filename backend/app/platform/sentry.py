"""Sentry integration (P7-T5): error tracking with correlation-id/org-id
context, disabled by default.

Imported lazily inside `init_sentry`, the same pattern `app.extraction.llm.
gemini`/`app.vision.ocr.paddle`/`app.reports.pdf` already use for optional
native/external dependencies - importing this module never requires the SDK
to be configured, and `sentry_sdk` itself is always installed (a normal
dependency, unlike WeasyPrint/PaddleOCR's native-library gap) so there is no
availability check needed, only a "is a DSN configured" one.
"""

from __future__ import annotations

from app.platform.config import Settings
from app.platform.logging import actor_id_var, correlation_id_var, org_id_var


def init_sentry(settings: Settings) -> None:
    """No-op when `LABELLENS_SENTRY_DSN` is unset - local/test runs and any
    deployment that hasn't configured Sentry yet send nothing, matching
    every other optional external dependency in this codebase."""
    if not settings.sentry_dsn:
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        # This is a compliance product that may see label text in stack
        # frames' local variables; sending request bodies/PII to a third
        # party by default would contradict section 20's own "never log
        # label text, secrets, or full prompts" rule for structured logs.
        send_default_pii=False,
        # No performance tracing - this task's job is error capture, not a
        # second APM tool alongside the metrics/logs already covering that.
        traces_sample_rate=0.0,
    )


def capture_exception(exc: BaseException) -> None:
    """Reports `exc` to Sentry if configured, tagged with the same
    correlation id / org id / actor id every structured log line already
    carries (`app.platform.logging.context_processor`) - so a Sentry issue
    and its corresponding log lines can always be cross-referenced by the
    same three values. A no-op if Sentry was never initialized."""
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - sentry-sdk is always installed
        return

    if not sentry_sdk.get_client().is_active():
        return

    with sentry_sdk.new_scope() as scope:
        for name, var in (
            ("correlation_id", correlation_id_var),
            ("org_id", org_id_var),
            ("actor_id", actor_id_var),
        ):
            value = var.get()
            if value is not None:
                scope.set_tag(name, value)
        sentry_sdk.capture_exception(exc)
