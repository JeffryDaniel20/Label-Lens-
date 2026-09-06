"""Arq job for P7-T2's async PDF rendering path.

Registered into `app.analysis.worker.WorkerSettings.functions` (the same
central worker every stage job already runs on - a second `WorkerSettings`
would mean a second worker process to deploy and monitor for no benefit).
Kept separate from `app.reports.service.render_and_store_pdf` itself, the
same split `app.analysis.stages`/`app.analysis.worker` already use: the
service function is the real, directly-callable, directly-testable work;
this module is only the queueing shell around it.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.session import get_session_factory, set_tenant_context
from app.platform.config import get_settings
from app.reports import service as reports_service
from app.storage.client import build_storage_client


async def render_report_pdf_job(ctx: dict[str, Any], report_id: str, organization_id: str) -> str:
    """Renders the PDF for an already-generated JSON `Report` and stores it
    as a new `kind="pdf"` row. Returns the new row's id."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        org_uuid = uuid.UUID(organization_id)
        set_tenant_context(db, org_uuid)  # RLS context (no-op on SQLite)
        json_report = reports_service.get_report(
            db, organization_id=org_uuid, report_id=uuid.UUID(report_id)
        )
        storage_client = build_storage_client(get_settings())
        pdf_report = reports_service.render_and_store_pdf(
            db, json_report=json_report, storage_client=storage_client
        )
        db.commit()
        return str(pdf_report.id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
