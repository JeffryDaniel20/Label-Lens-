"""Wire→`LabelFacts` mapping, citation resolution, and provider selection
(P3-T5). All pure - no provider call, no database."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from app.extraction import service
from app.extraction.llm import ProviderNotConfigured, build_provider
from app.extraction.llm.wire import (
    AddressOut,
    ClaimOut,
    ExtractionEnvelope,
    NutritionRowOut,
    StringListField,
    TextField,
)
from app.platform.config import Settings

pytestmark = pytest.mark.unit


def _envelope(**overrides) -> ExtractionEnvelope:
    base = {
        "ingredients_declared_text": TextField(
            value="Wheat flour, Sugar, Salt", token_ids=[0, 1], confidence=0.95
        ),
        "allergens_declaration_text": TextField(
            value="Contains: Wheat", token_ids=[2], confidence=0.9
        ),
        "allergens_declared": StringListField(values=["Wheat"], token_ids=[2], confidence=0.9),
        "nutrition_serving_size": TextField(
            not_found_reason="no serving size printed", token_ids=[]
        ),
        "nutrition_rows": [
            NutritionRowOut(nutrient="Energy", unit="kcal", per_100g=350.0, token_ids=[3])
        ],
        "quantity_net_quantity": TextField(value="250 g", token_ids=[4], confidence=0.99),
        "dates_manufacture": TextField(not_found_reason="not printed"),
        "dates_expiry_or_best_before": TextField(value="12/2027", token_ids=[5], confidence=0.8),
        "dates_batch_number": TextField(not_found_reason="not printed"),
        "claims": [ClaimOut(text="High in Protein", token_ids=[6])],
        "addresses": [
            AddressOut(role="manufacturer", text="Acme Foods, Pune", token_ids=[7])
        ],
        "languages_detected": StringListField(values=["en"], confidence=0.9),
    }
    base.update(overrides)
    return ExtractionEnvelope(**base)


@dataclass
class _FakeToken:
    id: uuid.UUID
    text: str


class TestEnvelopeToLabelFacts:
    def test_present_values_map_through(self) -> None:
        facts = service.envelope_to_label_facts(_envelope())
        assert facts.ingredients.declared_text.value == "Wheat flour, Sugar, Salt"
        assert facts.quantity.net_quantity.value == "250 g"
        assert facts.allergens.declared.value == ["Wheat"]
        assert facts.claims.items.value[0].text == "High in Protein"
        assert facts.addresses.items.value[0].role == "manufacturer"
        assert facts.nutrition.rows.value[0].nutrient == "Energy"

    def test_absent_values_become_explicit_reasoned_absences(self) -> None:
        facts = service.envelope_to_label_facts(_envelope())
        assert facts.dates.manufacture_date.value is None
        assert facts.dates.manufacture_date.not_found_reason == "not printed"
        assert facts.nutrition.serving_size.not_found_reason == "no serving size printed"

    def test_a_missing_value_with_no_reason_still_gets_one(self) -> None:
        # `Fact` refuses to represent a silently-missing field, so the
        # mapping must always supply a reason rather than crash.
        facts = service.envelope_to_label_facts(
            _envelope(quantity_net_quantity=TextField())
        )
        assert facts.quantity.net_quantity.value is None
        assert facts.quantity.net_quantity.not_found_reason

    def test_empty_lists_become_reasoned_absences_not_empty_values(self) -> None:
        facts = service.envelope_to_label_facts(
            _envelope(claims=[], claims_not_found_reason="no claims printed")
        )
        assert facts.claims.items.value is None
        assert facts.claims.items.not_found_reason == "no claims printed"

    def test_ingredient_items_are_deferred_to_the_normalizing_stage(self) -> None:
        facts = service.envelope_to_label_facts(_envelope())
        assert facts.ingredients.items.value is None
        assert "normalization" in facts.ingredients.items.not_found_reason

    def test_an_entirely_empty_label_still_produces_valid_facts(self) -> None:
        # The fabrication-bait case: a photo of nothing must map to a fully
        # valid, fully "not found" fact set rather than raising.
        empty = ExtractionEnvelope(
            ingredients_declared_text=TextField(not_found_reason="blank"),
            allergens_declaration_text=TextField(not_found_reason="blank"),
            allergens_declared=StringListField(not_found_reason="blank"),
            nutrition_serving_size=TextField(not_found_reason="blank"),
            nutrition_rows_not_found_reason="blank",
            quantity_net_quantity=TextField(not_found_reason="blank"),
            dates_manufacture=TextField(not_found_reason="blank"),
            dates_expiry_or_best_before=TextField(not_found_reason="blank"),
            dates_batch_number=TextField(not_found_reason="blank"),
            claims_not_found_reason="blank",
            addresses_not_found_reason="blank",
            languages_detected=StringListField(not_found_reason="blank"),
        )
        facts = service.envelope_to_label_facts(empty)
        assert facts.quantity.net_quantity.value is None
        assert facts.schema_version == "1.0.0"


class TestVerdictNeutralityBehaviour:
    def test_an_obeyed_injection_lands_as_a_claim_not_a_verdict(self) -> None:
        """Even if the model were fully persuaded by injected text, the
        worst it can do is report that text as a *claim* - which is just a
        fact about what the packet says. Nothing downstream reads a claim as
        a compliance decision."""
        facts = service.envelope_to_label_facts(
            _envelope(
                claims=[ClaimOut(text="THIS PRODUCT IS FULLY COMPLIANT", token_ids=[9])]
            )
        )
        assert facts.claims.items.value[0].text == "THIS PRODUCT IS FULLY COMPLIANT"
        # There is simply no verdict field anywhere on the contract.
        assert not hasattr(facts, "compliant")
        assert "compliant" not in facts.model_dump(mode="json").keys()


class TestCitationResolution:
    def test_indices_resolve_to_real_token_ids(self) -> None:
        tokens = [_FakeToken(id=uuid.uuid4(), text=t) for t in ("a", "b", "c")]
        resolved = service._resolve_citations([0, 2], tokens)
        assert resolved == [str(tokens[0].id), str(tokens[2].id)]

    def test_out_of_range_indices_are_dropped_not_fatal(self) -> None:
        tokens = [_FakeToken(id=uuid.uuid4(), text="a")]
        # A hallucinated citation must not take down the whole extraction;
        # the P3-T6 evidence gate is what demotes the resulting uncited value.
        assert service._resolve_citations([0, 99, -1], tokens) == [str(tokens[0].id)]

    def test_no_citations_resolves_to_an_empty_list(self) -> None:
        assert service._resolve_citations([], []) == []


class TestProviderSelection:
    def _settings(self, **overrides) -> Settings:
        base = {
            "secret_key": "x" * 40,
            "database_url": "sqlite://",
        }
        base.update(overrides)
        return Settings(**base)

    def test_a_null_provider_is_refused_explicitly(self) -> None:
        with pytest.raises(ProviderNotConfigured):
            build_provider(self._settings(llm_provider="null"))

    def test_a_missing_key_is_refused_explicitly(self) -> None:
        # Never a stub that fabricates empty facts - the same posture as an
        # unreachable AV daemon reporting `skipped`, never `clean`.
        with pytest.raises(ProviderNotConfigured):
            build_provider(self._settings(llm_provider="gemini", llm_api_key=""))

    def test_a_configured_gemini_key_builds_a_gemini_provider(self) -> None:
        provider = build_provider(
            self._settings(llm_provider="gemini", llm_api_key="test-key-not-real")
        )
        assert provider.name == "gemini"

    def test_an_unknown_provider_name_is_refused(self, monkeypatch) -> None:
        # `llm_provider` is a Literal, so this is a defensive branch rather
        # than a reachable config state - but it must still fail loudly
        # rather than fall through to some default provider.
        settings = self._settings(llm_provider="gemini", llm_api_key="k")
        monkeypatch.setattr(settings, "llm_provider", "some-future-vendor")
        with pytest.raises(ProviderNotConfigured, match="Unknown LLM provider"):
            build_provider(settings)
