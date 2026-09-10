"""P4-T6's scaffolding CLI (`scripts/new_rule.py`) - the literal acceptance
criterion this task exists to prove: "a new rule can be added, tested, and
published without touching Python." Every test here invokes the real CLI as
a subprocess (not by importing its internals), the same way a rule author
actually would, then proves the result through the real, unmodified loader
and evaluator (`app.rules.loader.load_pack_from_directory`,
`app.rules.evaluator.evaluate`) - not a hand-simplified stand-in for either.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.rules.evaluator import FindingStatus, evaluate
from app.rules.loader import load_pack_from_directory
from app.rules.schema import Rule

pytestmark = pytest.mark.unit

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND_DIR / "scripts" / "new_rule.py"


def _make_pack_dir(tmp_path: Path) -> Path:
    pack_dir = tmp_path / "in-test-food" / "1.0.0"
    (pack_dir / "rules").mkdir(parents=True)
    (pack_dir / "fixtures").mkdir(parents=True)
    manifest = {
        "pack_id": "in-test-food",
        "jurisdiction": "IN",
        "category": "packaged_food",
        "version": "1.0.0",
        "effective_from": "2022-07-01",
        "effective_to": None,
        "source_citations": ["Test citation, not real regulatory content"],
        "author": "test",
        "review_date": "2026-09-10",
    }
    (pack_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return pack_dir


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter/script path, test-controlled args
        [sys.executable, str(SCRIPT), *args],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )


class TestScaffoldingATextFieldRule:
    def test_scaffolds_a_valid_rule_and_its_fixtures_with_zero_python(
        self, tmp_path: Path
    ) -> None:
        pack_dir = _make_pack_dir(tmp_path)
        result = _run_cli(
            str(pack_dir),
            "IN-TEST-BATCH-DECLARED",
            "--field",
            "dates.batch_number",
            "--title",
            "Batch number is declared",
            "--citation",
            "Test citation, not real regulatory content",
        )
        assert result.returncode == 0, result.stderr

        rule_files = list((pack_dir / "rules").glob("*.yaml"))
        assert len(rule_files) == 1
        fixture_dir = pack_dir / "fixtures" / "IN-TEST-BATCH-DECLARED"
        assert {p.stem for p in fixture_dir.glob("*.json")} == {
            "pass",
            "fail",
            "insufficient_data",
        }

        # The generated rule is real, schema-valid content - loadable by the
        # exact same production code path every published pack goes through.
        pack = load_pack_from_directory(pack_dir)
        assert len(pack.rules) == 1
        rule = pack.rules[0]
        assert rule.rule_key == "IN-TEST-BATCH-DECLARED"
        assert rule.citation == "Test citation, not real regulatory content"

        # Every fixture the CLI generated evaluates to exactly the status
        # its own filename promises, through the real, unmodified evaluator.
        for case, expected in (
            ("pass", FindingStatus.PASS),
            ("fail", FindingStatus.FAIL),
            ("insufficient_data", FindingStatus.INSUFFICIENT_DATA),
        ):
            facts = json.loads((fixture_dir / f"{case}.json").read_text(encoding="utf-8"))
            findings = evaluate(
                facts, [rule], as_of=dt.date(2026, 9, 10),
                jurisdiction="IN", category="packaged_food",
            )
            assert findings[0].status is expected

    def test_the_generated_yaml_round_trips_through_the_real_rule_schema(
        self, tmp_path: Path
    ) -> None:
        """Not just "the loader accepts it" (covered above) - the raw YAML
        on disk, parsed independently, must validate as a `Rule` with no
        coercion this test itself performs."""
        pack_dir = _make_pack_dir(tmp_path)
        _run_cli(
            str(pack_dir),
            "IN-TEST-NET-QTY-DECLARED",
            "--field",
            "quantity.net_quantity",
            "--title",
            "Net quantity is declared",
            "--citation",
            "Test citation, not real regulatory content",
            "--severity",
            "critical",
        )
        rule_file = next((pack_dir / "rules").glob("*.yaml"))
        raw = yaml.safe_load(rule_file.read_text(encoding="utf-8"))
        rule = Rule.model_validate(raw)
        assert rule.severity.value == "critical"
        assert rule.evidence.fields == ["quantity.net_quantity"]


class TestScaffoldingAListFieldRule:
    def test_a_list_shaped_field_gets_only_pass_and_insufficient_data_fixtures(
        self, tmp_path: Path
    ) -> None:
        """`field_present` cannot distinguish "declared" from "declared but
        empty" for a list - the CLI must not fabricate an unreachable `fail`
        fixture for this shape, matching the same honest limitation the real
        `in-fssai-food` pack states for its own list-shaped rules."""
        pack_dir = _make_pack_dir(tmp_path)
        result = _run_cli(
            str(pack_dir),
            "IN-TEST-ADDRESS-DECLARED",
            "--field",
            "addresses.items",
            "--title",
            "Address is declared",
            "--citation",
            "Test citation, not real regulatory content",
        )
        assert result.returncode == 0, result.stderr

        fixture_dir = pack_dir / "fixtures" / "IN-TEST-ADDRESS-DECLARED"
        assert {p.stem for p in fixture_dir.glob("*.json")} == {"pass", "insufficient_data"}

        pack = load_pack_from_directory(pack_dir)
        rule = pack.rules[0]
        assert "not currently reachable in practice" in rule.citation
        for case, expected in (
            ("pass", FindingStatus.PASS),
            ("insufficient_data", FindingStatus.INSUFFICIENT_DATA),
        ):
            facts = json.loads((fixture_dir / f"{case}.json").read_text(encoding="utf-8"))
            findings = evaluate(
                facts, [rule], as_of=dt.date(2026, 9, 10),
                jurisdiction="IN", category="packaged_food",
            )
            assert findings[0].status is expected


class TestCliRejectsBadInputBeforeWritingAnything:
    def test_rejects_a_malformed_rule_key(self, tmp_path: Path) -> None:
        pack_dir = _make_pack_dir(tmp_path)
        result = _run_cli(
            str(pack_dir),
            "not-a-good-key",
            "--field",
            "dates.batch_number",
            "--title",
            "x",
            "--citation",
            "y",
        )
        assert result.returncode != 0
        assert "must be upper-snake segments" in result.stderr
        assert list((pack_dir / "rules").glob("*.yaml")) == []

    def test_rejects_a_malformed_field_path(self, tmp_path: Path) -> None:
        pack_dir = _make_pack_dir(tmp_path)
        result = _run_cli(
            str(pack_dir),
            "IN-TEST-FOO",
            "--field",
            "NotAValidPath",
            "--title",
            "x",
            "--citation",
            "y",
        )
        assert result.returncode != 0
        assert "must be a dotted lower_snake_case path" in result.stderr
        assert list((pack_dir / "rules").glob("*.yaml")) == []

    def test_refuses_to_overwrite_an_existing_rule_key(self, tmp_path: Path) -> None:
        pack_dir = _make_pack_dir(tmp_path)
        first = _run_cli(
            str(pack_dir),
            "IN-TEST-DUP",
            "--field",
            "dates.batch_number",
            "--title",
            "x",
            "--citation",
            "y",
        )
        assert first.returncode == 0, first.stderr

        second = _run_cli(
            str(pack_dir),
            "IN-TEST-DUP",
            "--field",
            "dates.manufacture_date",
            "--title",
            "x2",
            "--citation",
            "y2",
        )
        assert second.returncode != 0
        assert "already appears to exist" in second.stderr
        assert len(list((pack_dir / "rules").glob("*.yaml"))) == 1


class TestScaffoldedRuleFileNumberingIsSequential:
    def test_a_second_rule_gets_the_next_number_not_a_collision(self, tmp_path: Path) -> None:
        pack_dir = _make_pack_dir(tmp_path)
        _run_cli(
            str(pack_dir), "IN-TEST-A", "--field", "dates.batch_number",
            "--title", "x", "--citation", "y",
        )
        _run_cli(
            str(pack_dir), "IN-TEST-B", "--field", "dates.manufacture_date",
            "--title", "x2", "--citation", "y2",
        )
        names = sorted(p.name for p in (pack_dir / "rules").glob("*.yaml"))
        assert names == ["01_in_test_a.yaml", "02_in_test_b.yaml"]
