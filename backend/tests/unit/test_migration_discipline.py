"""P7-T7: the expand/migrate/contract discipline checker
(`scripts/check_migration_discipline.py`) actually catches what it claims
to, and the real repository's own migrations are genuinely clean under it -
not just asserted, run for real against the real `migrations/versions`
directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import check_migration_discipline as checker  # noqa: E402

pytestmark = pytest.mark.unit

REAL_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


NOT_NULL_NO_DEFAULT = """
import sqlalchemy as sa
from alembic import op

def upgrade() -> None:
    op.add_column("widgets", sa.Column("required_flag", sa.Boolean(), nullable=False))

def downgrade() -> None:
    op.drop_column("widgets", "required_flag")
"""

NOT_NULL_WITH_DEFAULT = """
import sqlalchemy as sa
from alembic import op

def upgrade() -> None:
    op.add_column(
        "widgets",
        sa.Column("required_flag", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

def downgrade() -> None:
    op.drop_column("widgets", "required_flag")
"""

NULLABLE_COLUMN = """
import sqlalchemy as sa
from alembic import op

def upgrade() -> None:
    op.add_column("widgets", sa.Column("optional_flag", sa.Boolean(), nullable=True))
"""

DROP_COLUMN_IN_UPGRADE = """
from alembic import op

def upgrade() -> None:
    op.drop_column("widgets", "legacy_flag")
"""

DROP_COLUMN_MARKED_CONTRACT_OK = """
from alembic import op

def upgrade() -> None:
    op.drop_column("widgets", "legacy_flag")  # contract-ok: deferred from 0031, one release ago
"""

RENAME_TABLE_IN_UPGRADE = """
from alembic import op

def upgrade() -> None:
    op.rename_table("widgets", "gadgets")
"""

ALTER_COLUMN_RENAME = """
from alembic import op

def upgrade() -> None:
    op.alter_column("widgets", "old_name", new_column_name="new_name")
"""

DROP_IN_DOWNGRADE_ONLY_IS_FINE = """
import sqlalchemy as sa
from alembic import op

def upgrade() -> None:
    op.add_column("widgets", sa.Column("new_flag", sa.Boolean(), nullable=True))

def downgrade() -> None:
    op.drop_column("widgets", "new_flag")
"""

BATCH_ALTER_ADD_COLUMN_NOT_NULL_NO_DEFAULT = """
import sqlalchemy as sa
from alembic import op

def upgrade() -> None:
    with op.batch_alter_table("widgets") as batch_op:
        batch_op.add_column(sa.Column("required_flag", sa.Boolean(), nullable=False))
"""


class TestCatchesRealViolations:
    def test_not_null_without_default_is_flagged(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_bad.py", NOT_NULL_NO_DEFAULT)
        violations = checker.check_directory(tmp_path)
        assert len(violations) == 1
        assert "nullable=False and no server_default" in violations[0].message

    def test_the_same_violation_through_batch_alter_table_is_also_flagged(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "0001_bad.py", BATCH_ALTER_ADD_COLUMN_NOT_NULL_NO_DEFAULT)
        violations = checker.check_directory(tmp_path)
        assert len(violations) == 1

    def test_dropping_a_column_in_upgrade_is_flagged(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_bad.py", DROP_COLUMN_IN_UPGRADE)
        violations = checker.check_directory(tmp_path)
        assert len(violations) == 1
        assert "contract operation" in violations[0].message

    def test_renaming_a_table_in_upgrade_is_flagged(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_bad.py", RENAME_TABLE_IN_UPGRADE)
        violations = checker.check_directory(tmp_path)
        assert len(violations) == 1

    def test_renaming_a_column_via_alter_column_is_flagged(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_bad.py", ALTER_COLUMN_RENAME)
        violations = checker.check_directory(tmp_path)
        assert len(violations) == 1
        assert "renames a column" in violations[0].message


class TestRealPatternsPass:
    def test_a_default_backed_not_null_column_is_fine(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_ok.py", NOT_NULL_WITH_DEFAULT)
        assert checker.check_directory(tmp_path) == []

    def test_a_nullable_column_needs_no_default(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_ok.py", NULLABLE_COLUMN)
        assert checker.check_directory(tmp_path) == []

    def test_dropping_a_column_only_in_downgrade_is_fine(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_ok.py", DROP_IN_DOWNGRADE_ONLY_IS_FINE)
        assert checker.check_directory(tmp_path) == []


class TestContractOkEscapeHatch:
    def test_a_marked_contract_step_is_not_flagged(self, tmp_path: Path) -> None:
        _write(tmp_path, "0001_ok.py", DROP_COLUMN_MARKED_CONTRACT_OK)
        assert checker.check_directory(tmp_path) == []


class TestTheRealRepository:
    def test_every_real_migration_is_actually_clean(self) -> None:
        assert REAL_MIGRATIONS_DIR.is_dir(), "expected the real migrations/versions directory"
        violations = checker.check_directory(REAL_MIGRATIONS_DIR)
        assert violations == [], "\n".join(str(v) for v in violations)
