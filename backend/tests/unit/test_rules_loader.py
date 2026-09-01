"""Unit tests for rule-pack loading and checksumming (P4-T1).

No live services needed: file-based tests use `tmp_path`, not real storage.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from app.rules.loader import (
    PackValidationError,
    RulePack,
    compute_pack_checksum,
    load_pack,
    load_pack_from_directory,
)
from app.rules.schema import PackManifest, Rule

pytestmark = pytest.mark.unit

VALID_RULE: dict[str, object] = {
    "rule_key": "IN-FSSAI-FOOD-ALLERGEN-DECL",
    "version": 3,
    "title": "Declaration of major allergens",
    "citation": "FSS (Labelling & Display) Regulations, 2020 — reg. 5(6)",
    "severity": "critical",
    "effective_from": "2020-07-01",
    "applicability": {
        "jurisdiction": ["IN"],
        "category": ["packaged_food"],
        "predicates": [{"field_present": "ingredients.items"}],
    },
    "logic": {"all": [{"field_present": "allergens.declared"}]},
    "requires_fields": ["ingredients.items", "allergens.declared"],
    "on_missing_fields": "insufficient_data",
    "message": {
        "fail": "Allergen appears in ingredients but is not declared.",
        "insufficient_data": "Ingredient list could not be read with sufficient confidence.",
    },
    "evidence": {"fields": ["ingredients.items", "allergens.declared"]},
}

SECOND_RULE: dict[str, object] = {
    "rule_key": "IN-FSSAI-FOOD-NET-QTY",
    "version": 1,
    "title": "Net quantity declaration",
    "citation": "Legal Metrology (Packaged Commodities) Rules, 2011 — rule 6",
    "severity": "major",
    "effective_from": "2011-01-01",
    "applicability": {
        "jurisdiction": ["IN"],
        "category": ["packaged_food"],
        "predicates": [],
    },
    "logic": {"field_present": "quantity.net_quantity"},
    "requires_fields": ["quantity.net_quantity"],
    "on_missing_fields": "insufficient_data",
    "message": {
        "fail": "Net quantity is not declared.",
        "insufficient_data": "Net quantity could not be read with sufficient confidence.",
    },
    "evidence": {"fields": ["quantity.net_quantity"]},
}

VALID_MANIFEST: dict[str, object] = {
    "pack_id": "in-fssai-food",
    "jurisdiction": "IN",
    "category": "packaged_food",
    "version": "1.0.0",
    "effective_from": "2024-01-01",
    "source_citations": ["FSS (Labelling & Display) Regulations, 2020"],
    "author": "test-author",
    "review_date": "2024-01-01",
}


class TestLoadPack:
    def test_a_valid_manifest_and_rules_load_successfully(self) -> None:
        pack = load_pack(VALID_MANIFEST, [VALID_RULE, SECOND_RULE])
        assert isinstance(pack, RulePack)
        assert pack.manifest.pack_id == "in-fssai-food"
        assert {r.rule_key for r in pack.rules} == {
            "IN-FSSAI-FOOD-ALLERGEN-DECL",
            "IN-FSSAI-FOOD-NET-QTY",
        }
        assert len(pack.checksum) == 64  # sha256 hex digest

    def test_an_empty_pack_is_rejected(self) -> None:
        with pytest.raises(PackValidationError) as exc_info:
            load_pack(VALID_MANIFEST, [])
        assert any("at least one rule" in issue for issue in exc_info.value.issues)

    def test_a_broken_manifest_is_rejected(self) -> None:
        broken = dict(VALID_MANIFEST)
        broken["pack_id"] = "not a valid id"
        with pytest.raises(PackValidationError) as exc_info:
            load_pack(broken, [VALID_RULE])
        assert any("pack_id" in issue for issue in exc_info.value.issues)

    def test_a_broken_rule_is_rejected_with_its_rule_key_in_the_error(self) -> None:
        broken = dict(VALID_RULE)
        broken["severity"] = "not_a_real_severity"
        with pytest.raises(PackValidationError) as exc_info:
            load_pack(VALID_MANIFEST, [broken])
        assert any("IN-FSSAI-FOOD-ALLERGEN-DECL" in issue for issue in exc_info.value.issues)

    def test_duplicate_rule_key_is_rejected(self) -> None:
        duplicate = dict(VALID_RULE)
        with pytest.raises(PackValidationError) as exc_info:
            load_pack(VALID_MANIFEST, [VALID_RULE, duplicate])
        assert any("duplicate rule_key" in issue for issue in exc_info.value.issues)

    def test_every_problem_is_reported_not_just_the_first(self) -> None:
        broken_manifest = dict(VALID_MANIFEST)
        broken_manifest["pack_id"] = "bad id"
        broken_rule = dict(VALID_RULE)
        broken_rule["severity"] = "nonsense"
        with pytest.raises(PackValidationError) as exc_info:
            load_pack(broken_manifest, [broken_rule])
        # Both the manifest problem and the rule problem are surfaced together.
        assert len(exc_info.value.issues) >= 2

    def test_an_invalid_pack_never_produces_a_rulepack(self) -> None:
        """The literal acceptance criterion: an invalid pack cannot be
        published, because it never successfully loads in the first place."""
        with pytest.raises(PackValidationError):
            load_pack(VALID_MANIFEST, [{"rule_key": "INCOMPLETE"}])


class TestChecksumStability:
    def test_identical_content_produces_the_same_checksum(self) -> None:
        pack1 = load_pack(VALID_MANIFEST, [VALID_RULE, SECOND_RULE])
        pack2 = load_pack(copy.deepcopy(VALID_MANIFEST), copy.deepcopy([VALID_RULE, SECOND_RULE]))
        assert pack1.checksum == pack2.checksum

    def test_rule_order_does_not_affect_the_checksum(self) -> None:
        pack1 = load_pack(VALID_MANIFEST, [VALID_RULE, SECOND_RULE])
        pack2 = load_pack(VALID_MANIFEST, [SECOND_RULE, VALID_RULE])
        assert pack1.checksum == pack2.checksum

    def test_dict_key_order_does_not_affect_the_checksum(self) -> None:
        reordered_rule = {key: VALID_RULE[key] for key in reversed(list(VALID_RULE))}
        pack1 = load_pack(VALID_MANIFEST, [VALID_RULE])
        pack2 = load_pack(VALID_MANIFEST, [reordered_rule])
        assert pack1.checksum == pack2.checksum

    def test_a_real_content_change_changes_the_checksum(self) -> None:
        changed_rule = dict(VALID_RULE)
        changed_rule["version"] = 4
        pack1 = load_pack(VALID_MANIFEST, [VALID_RULE])
        pack2 = load_pack(VALID_MANIFEST, [changed_rule])
        assert pack1.checksum != pack2.checksum

    def test_a_manifest_change_changes_the_checksum(self) -> None:
        changed_manifest = dict(VALID_MANIFEST)
        changed_manifest["version"] = "1.0.1"
        pack1 = load_pack(VALID_MANIFEST, [VALID_RULE])
        pack2 = load_pack(changed_manifest, [VALID_RULE])
        assert pack1.checksum != pack2.checksum

    def test_compute_pack_checksum_matches_the_loaded_packs_checksum(self) -> None:
        manifest = PackManifest.model_validate(VALID_MANIFEST)
        rules = [Rule.model_validate(VALID_RULE)]
        assert compute_pack_checksum(manifest, rules) == load_pack(
            VALID_MANIFEST, [VALID_RULE]
        ).checksum


class TestLoadPackFromDirectory:
    def test_loads_a_pack_from_manifest_and_rules_files(self, tmp_path) -> None:
        pack_dir = tmp_path / "in-fssai-food"
        (pack_dir / "rules").mkdir(parents=True)
        (pack_dir / "manifest.yaml").write_text(yaml.safe_dump(VALID_MANIFEST), encoding="utf-8")
        (pack_dir / "rules" / "allergen-decl.yaml").write_text(
            yaml.safe_dump(VALID_RULE), encoding="utf-8"
        )
        (pack_dir / "rules" / "net-qty.yaml").write_text(
            yaml.safe_dump(SECOND_RULE), encoding="utf-8"
        )

        pack = load_pack_from_directory(pack_dir)
        assert pack.manifest.pack_id == "in-fssai-food"
        assert len(pack.rules) == 2

    def test_a_missing_manifest_is_rejected(self, tmp_path) -> None:
        pack_dir = tmp_path / "empty-pack"
        pack_dir.mkdir()
        with pytest.raises(PackValidationError, match="manifest"):
            load_pack_from_directory(pack_dir)

    def test_a_pack_with_no_rules_directory_is_rejected(self, tmp_path) -> None:
        pack_dir = tmp_path / "no-rules"
        pack_dir.mkdir()
        (pack_dir / "manifest.yaml").write_text(yaml.safe_dump(VALID_MANIFEST), encoding="utf-8")
        with pytest.raises(PackValidationError):
            load_pack_from_directory(pack_dir)
