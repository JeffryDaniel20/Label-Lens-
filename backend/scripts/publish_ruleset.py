"""Publish a rule pack directory (manifest.yaml + rules/*.yaml) into the real
`rulesets`/`rules` tables (P4-T4's `publish_pack`) so `_rule_eval`
(`app.analysis.stages`) can actually find and evaluate it.

This is the one manual step nothing in the analysis pipeline itself
performs automatically, by design: publishing regulatory content is a
deliberate, auditable operator action (IMPLEMENTATION.md section 10), never
a side effect of running an analysis. Run this once per environment (it is
idempotent - re-running it against the same pack content is a no-op, see
`publish_pack`'s own docstring) after `LABELLENS_DATABASE_URL` points at a
real database:

    python scripts/publish_ruleset.py app/rulesets/in-fssai-food/v1.0.0
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.db.session import init_engine, session_scope
from app.platform.config import get_settings
from app.rules.loader import load_pack_from_directory
from app.rules.publish import publish_pack


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <pack_directory>", file=sys.stderr)
        raise SystemExit(2)

    pack_dir = Path(sys.argv[1]).resolve()
    pack = load_pack_from_directory(pack_dir)

    settings = get_settings()
    init_engine(settings.database_url)
    with session_scope() as db:
        ruleset = publish_pack(db, pack)
        db.commit()

    print(
        f"Published {pack.manifest.pack_id} v{pack.manifest.version} "
        f"({len(pack.rules)} rules) as ruleset {ruleset.id} "
        f"(checksum {ruleset.checksum[:12]}...)."
    )


if __name__ == "__main__":
    main()
