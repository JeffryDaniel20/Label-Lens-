"""Unit tests for the pure half of P7-T1: the snapshot hash's stability
guarantee. The DB-touching half (`build_snapshot`/`persist_report`) has its
own integration suite in `tests/integration/test_reports_service.py`.
"""

from __future__ import annotations

import pytest

from app.reports.service import compute_snapshot_hash

pytestmark = pytest.mark.unit


def _snapshot(**cover_overrides: object) -> dict[str, object]:
    cover = {
        "analysis_id": "a1",
        "generated_at": None,
        "generated_by": None,
        "report_hash": None,
    }
    cover.update(cover_overrides)
    return {"schema_version": "1.0.0", "cover": cover, "findings": [{"rule_key": "X"}]}


class TestComputeSnapshotHash:
    def test_is_stable_across_different_generated_at_values(self) -> None:
        a = compute_snapshot_hash(_snapshot(generated_at="2026-01-01T00:00:00Z"))
        b = compute_snapshot_hash(_snapshot(generated_at="2026-06-01T00:00:00Z"))
        assert a == b

    def test_is_stable_across_different_generated_by_values(self) -> None:
        a = compute_snapshot_hash(_snapshot(generated_by="user-1"))
        b = compute_snapshot_hash(_snapshot(generated_by="user-2"))
        assert a == b

    def test_ignores_its_own_report_hash_field(self) -> None:
        a = compute_snapshot_hash(_snapshot(report_hash=None))
        b = compute_snapshot_hash(_snapshot(report_hash="some-previous-hash"))
        assert a == b

    def test_changes_when_real_content_changes(self) -> None:
        a = compute_snapshot_hash(_snapshot())
        changed = _snapshot()
        changed["findings"] = [{"rule_key": "Y"}]
        b = compute_snapshot_hash(changed)
        assert a != b

    def test_is_insensitive_to_key_order(self) -> None:
        first = {"b": 2, "a": 1, "cover": {"generated_at": None, "generated_by": None}}
        second = {"a": 1, "b": 2, "cover": {"generated_by": None, "generated_at": None}}
        assert compute_snapshot_hash(first) == compute_snapshot_hash(second)
