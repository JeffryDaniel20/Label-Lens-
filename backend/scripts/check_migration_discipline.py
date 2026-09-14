"""Statically enforce IMPLEMENTATION.md section 25's expand/migrate/contract
discipline (P7-T7): "every migration must be backward-compatible with the
previous app version (expand -> migrate -> contract) so rollback never
requires a down-migration."

`infra/scripts/deploy.sh` runs every pending migration *before* swapping
containers to the new image, and `rollback.sh` only ever redeploys the
*previous* image against whatever schema is currently live - it never runs
`alembic downgrade`. That design is only safe if every migration's own
`upgrade()` is something the *previous* app version can tolerate. Two
concrete violations of that would break it silently:

1. Adding a `NOT NULL` column with no `server_default` - the previous app
   version's own `INSERT` statements don't know the new column exists, so
   every one of them would start failing the instant the migration runs,
   *before* the new app version is even deployed.
2. Dropping/renaming a column or table, or renaming a table, in the same
   migration that adds whatever replaces it - the previous app version is
   still reading/writing the old name, so a rollback after such a migration
   would resurrect an app version that queries a column that no longer
   exists.

Only `upgrade()` is scanned - `downgrade()` exists for local development and
the CI up/down check (section 26), never for a production rollback, so it is
free to drop what it added.

A single-line `# contract-ok: <reason>` comment on the offending line is the
deliberate escape hatch for an actual, intentional contract step (one
deferred at least one release after the expand it completes, per the
objective) - this script is a guardrail against an *accidental* violation,
not an absolute prohibition.

Usage: python scripts/check_migration_discipline.py [migrations/versions]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_CONTRACT_OK_MARKER = "# contract-ok:"
_CONTRACT_OPS = {"drop_column", "drop_table", "rename_table"}


class Violation:
    def __init__(self, path: Path, line: int, message: str) -> None:
        self.path = path
        self.line = line
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_falsy_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _line_has_contract_ok(source_lines: list[str], lineno: int) -> bool:
    # A multi-line call's marker may sit on any of its lines; check the
    # call's own start line through a few lines after it defensively.
    for offset in range(0, 6):
        idx = lineno - 1 + offset
        if 0 <= idx < len(source_lines) and _CONTRACT_OK_MARKER in source_lines[idx]:
            return True
    return False


def _find_upgrade(tree: ast.Module) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade":
            return node
    return None


def check_file(path: Path) -> list[Violation]:
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    upgrade = _find_upgrade(tree)
    if upgrade is None:
        return []

    violations: list[Violation] = []
    for node in ast.walk(upgrade):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name is None:
            continue

        if name == "add_column":
            column_arg = node.args[-1] if node.args else None
            if isinstance(column_arg, ast.Call) and _call_name(column_arg) == "Column":
                nullable = _keyword(column_arg, "nullable")
                server_default = _keyword(column_arg, "server_default")
                # SQLAlchemy's own default is `nullable=True`, so only an
                # *explicit* `nullable=False` is the thing to check.
                if _is_falsy_literal(nullable) and server_default is None:
                    if not _line_has_contract_ok(source_lines, node.lineno):
                        violations.append(
                            Violation(
                                path,
                                node.lineno,
                                "add_column with nullable=False and no server_default breaks "
                                "the previous app version's own inserts before it's even "
                                "replaced - add a server_default, or mark "
                                f"'{_CONTRACT_OK_MARKER} ...' if this is a deliberate, "
                                "already-deferred contract step.",
                            )
                        )

        elif name in _CONTRACT_OPS:
            if not _line_has_contract_ok(source_lines, node.lineno):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        f"{name}() in upgrade() is a contract operation - the previous app "
                        "version may still read/write it, so a rollback after this migration "
                        f"would break. Mark '{_CONTRACT_OK_MARKER} ...' if this is a "
                        "deliberate, already-deferred contract step.",
                    )
                )

        elif name == "alter_column" and _keyword(node, "new_column_name") is not None:
            if not _line_has_contract_ok(source_lines, node.lineno):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        "alter_column(new_column_name=...) renames a column the previous app "
                        f"version still refers to by its old name. Mark '{_CONTRACT_OK_MARKER} "
                        "...' if this is a deliberate, already-deferred contract step.",
                    )
                )

    return violations


def check_directory(directory: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        violations.extend(check_file(path))
    return violations


def main(argv: list[str]) -> int:
    directory = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / (
        "migrations/versions"
    )
    violations = check_directory(directory)
    if violations:
        print(f"Expand/migrate/contract discipline violations in {directory}:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print(f"No expand/migrate/contract discipline violations found in {directory}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
