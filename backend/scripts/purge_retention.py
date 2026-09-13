"""Nightly retention/deletion purge job (P7-T8).

Runs, in order, against the real configured database and object store:

1. `purge_expired_files` - deletes source-file bytes past each org's own
   `retention_days` (findings, evidence spans, audit rows and reports are
   never touched - see `app.retention.service`'s own module docstring).
2. `purge_deleted_organizations` / `purge_deleted_products` - hard-purges
   anything soft-deleted more than 30 days ago (the two-phase deletion's
   phase two), deleting every real object-storage key first.

Intended to be invoked once nightly (cron, a scheduled container, etc.):

    python scripts/purge_retention.py
"""

from __future__ import annotations

from app.db.session import init_engine, session_scope
from app.platform.config import get_settings
from app.retention.service import (
    purge_deleted_organizations,
    purge_deleted_products,
    purge_expired_files,
)
from app.storage.client import build_storage_client


def main() -> None:
    settings = get_settings()
    init_engine(settings.database_url)
    storage_client = build_storage_client(settings)

    with session_scope() as db:
        purged_files = purge_expired_files(db, storage_client=storage_client)
        db.commit()

        purged_products = purge_deleted_products(db, storage_client=storage_client)
        db.commit()

        purged_orgs = purge_deleted_organizations(db, storage_client=storage_client)
        db.commit()

    print(
        f"retention purge: {len(purged_files)} file(s), "
        f"{len(purged_products)} product(s), {len(purged_orgs)} organization(s)"
    )


if __name__ == "__main__":
    main()
