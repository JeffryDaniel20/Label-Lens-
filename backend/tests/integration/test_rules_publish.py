"""Integration tests for ruleset publishing, pinning, and loading (P4-T4).

Runs against SQLite (the shared `db` fixture) for publish/idempotency/
byte-identical-re-evaluation, since none of that depends on PostgreSQL. The
immutability guarantee is database-enforced (a trigger, migration 0005), so
that part is verified separately, against a live PostgreSQL container, in
`test_migrations_and_rls.py::TestPostgresGuarantees` (the file that already
centralizes every other Postgres-only guarantee) via
`test_rulesets_and_rules_reject_update_and_delete`.
"""

from __future__ import annotations

import copy
import uuid
from datetime import date

import pytest

from app.platform.errors import Conflict, NotFound
from app.rules.evaluator import evaluate
from app.rules.loader import load_pack
from app.rules.publish import load_ruleset, publish_pack

pytestmark = pytest.mark.integration

MANIFEST: dict[str, object] = {
    "pack_id": "in-fssai-food",
    "jurisdiction": "IN",
    "category": "packaged_food",
    "version": "1.0.0",
    "effective_from": "2024-01-01",
    "source_citations": ["FSS (Labelling & Display) Regulations, 2020"],
    "author": "test-author",
    "review_date": "2024-01-01",
}

NET_QTY_RULE: dict[str, object] = {
    "rule_key": "IN-TEST-NET-QTY",
    "version": 1,
    "title": "Net quantity declaration",
    "citation": "Legal Metrology (Packaged Commodities) Rules, 2011 — rule 6",
    "severity": "major",
    "effective_from": "2020-01-01",
    "applicability": {"jurisdiction": ["IN"], "category": ["packaged_food"], "predicates": []},
    "logic": {"field_present": "quantity.net_quantity"},
    "requires_fields": ["quantity.net_quantity"],
    "on_missing_fields": "insufficient_data",
    "message": {
        "fail": "Net quantity is not declared.",
        "insufficient_data": "Net quantity could not be read.",
    },
    "evidence": {"fields": ["quantity.net_quantity"]},
}

FACTS = {"quantity": {"net_quantity": {"value": "250 g", "not_found_reason": None}}}


class TestPublishPack:
    def test_publishing_a_valid_pack_persists_the_ruleset_and_its_rules(self, db) -> None:
        pack = load_pack(MANIFEST, [NET_QTY_RULE])
        ruleset = publish_pack(db, pack)
        db.commit()

        assert ruleset.jurisdiction == "IN"
        assert ruleset.category == "packaged_food"
        assert ruleset.checksum == pack.checksum

        reloaded, rules = load_ruleset(db, ruleset.id)
        assert reloaded.id == ruleset.id
        assert [r.rule_key for r in rules] == ["IN-TEST-NET-QTY"]

    def test_publishing_the_identical_pack_twice_is_idempotent(self, db) -> None:
        pack = load_pack(MANIFEST, [NET_QTY_RULE])
        first = publish_pack(db, pack)
        db.commit()
        second = publish_pack(db, pack)
        db.commit()
        assert first.id == second.id

        _, rules = load_ruleset(db, first.id)
        assert len(rules) == 1  # not duplicated

    def test_publishing_different_content_under_an_already_used_version_is_rejected(
        self, db
    ) -> None:
        pack = load_pack(MANIFEST, [NET_QTY_RULE])
        publish_pack(db, pack)
        db.commit()

        changed_rule = copy.deepcopy(NET_QTY_RULE)
        changed_rule["version"] = 2
        conflicting_pack = load_pack(MANIFEST, [changed_rule])  # same jur/cat/version
        with pytest.raises(Conflict):
            publish_pack(db, conflicting_pack)

    def test_loading_an_unknown_ruleset_id_is_not_found(self, db) -> None:
        with pytest.raises(NotFound):
            load_ruleset(db, uuid.uuid4())

    def test_different_versions_of_the_same_pack_publish_independently(self, db) -> None:
        pack_v1 = load_pack(MANIFEST, [NET_QTY_RULE])
        manifest_v2 = {**MANIFEST, "version": "1.1.0"}
        pack_v2 = load_pack(manifest_v2, [NET_QTY_RULE])

        ruleset_v1 = publish_pack(db, pack_v1)
        ruleset_v2 = publish_pack(db, pack_v2)
        db.commit()

        assert ruleset_v1.id != ruleset_v2.id
        assert ruleset_v1.version == "1.0.0"
        assert ruleset_v2.version == "1.1.0"


class TestByteIdenticalReEvaluation:
    """The literal acceptance criterion: re-running an old analysis
    reproduces its findings byte-for-byte, even after a newer ruleset has
    since been published for the same jurisdiction/category."""

    def test_a_pinned_ruleset_is_unaffected_by_a_later_publish(self, db) -> None:
        pack_v1 = load_pack(MANIFEST, [NET_QTY_RULE])
        ruleset_v1 = publish_pack(db, pack_v1)
        db.commit()

        as_of = date(2024, 6, 1)  # "3 months ago", from this test's point of view
        _, rules_v1 = load_ruleset(db, ruleset_v1.id)
        findings_then = evaluate(
            FACTS, rules_v1, as_of=as_of, jurisdiction="IN", category="packaged_food"
        )

        # "3 months later": a new ruleset version is published, with a rule
        # that would produce a *different* finding if it were used instead.
        changed_rule = copy.deepcopy(NET_QTY_RULE)
        changed_rule["version"] = 2
        changed_rule["logic"] = {"not": {"field_present": "quantity.net_quantity"}}
        manifest_v2 = {**MANIFEST, "version": "2.0.0"}
        pack_v2 = load_pack(manifest_v2, [changed_rule])
        publish_pack(db, pack_v2)
        db.commit()

        # Re-load the *pinned* v1 ruleset by its id (never "latest") and
        # re-evaluate with the identical facts and as_of.
        _, rules_v1_again = load_ruleset(db, ruleset_v1.id)
        findings_now = evaluate(
            FACTS, rules_v1_again, as_of=as_of, jurisdiction="IN", category="packaged_food"
        )

        assert findings_now == findings_then
        assert findings_then[0].status.value == "pass"  # sanity: not vacuously equal empties
