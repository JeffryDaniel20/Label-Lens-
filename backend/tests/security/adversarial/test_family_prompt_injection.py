"""Section 23, "Prompt injection" family.

Expected behaviour, verbatim: *verdicts unchanged, injection flagged, no
tool/network action.*

`tests/unit/test_extraction_prompt.py` already proves the prompt-level
primitives (the untrusted-data framing, delimiter forgery neutralisation,
the verdict-shaped-field-free schema). This module asserts the thing those
primitives exist to guarantee: that a label carrying each corpus payload,
run through the entire real pipeline, produces byte-identical findings to
the same label without it.

**"Injection flagged", stated honestly.** This codebase has no injection
*detector* and this task did not add one: a flag is a detection heuristic,
and a heuristic that misses (homoglyphs, novel phrasings, a language nobody
enumerated) invites exactly the false confidence section 23 exists to
prevent. What it has instead is the structural guarantee that makes the
flag non-load-bearing - the model cannot express a verdict (no
verdict-shaped field in the schema), cannot act (no tools, no network in
the provider call), and cannot smuggle an unbacked value past the P3-T6
evidence gate. The tests below assert that structural guarantee directly.
The gap is recorded in TESTTEST.md rather than papered over with a
`flagged=True` column nothing trustworthy would ever set.
"""

from __future__ import annotations

import json

import pytest

from app.extraction.llm import prompt as prompt_module
from app.extraction.llm.wire import ExtractionEnvelope
from tests.security.adversarial.corpus import load_injection_corpus, payloads_for_surface
from tests.security.adversarial.rig import (
    BASELINE_TOKENS,
    adversarial_case,
    run_adversarial,
)

pytestmark = [pytest.mark.security, pytest.mark.integration]

_OCR_PAYLOADS = payloads_for_surface("ocr")


class TestCorpusItself:
    def test_the_corpus_is_versioned_and_non_empty(self) -> None:
        version, payloads = load_injection_corpus()
        assert version == "injection-v1"
        assert len(payloads) >= 12
        # Every family section 23 names for this row is represented.
        families = {p.family for p in payloads}
        assert {
            "direct_instruction",
            "system_prefix",
            "fake_json",
            "delimiter_forgery",
            "homoglyph",
            "rtl_override",
            "base64_blob",
            "hidden_text",
        } <= families

    def test_every_surface_section_23_names_is_covered(self) -> None:
        _version, payloads = load_injection_corpus()
        assert {p.surface for p in payloads} == {"ocr", "filename", "pdf_metadata"}


class TestVerdictsAreUnchangedByInjection:
    """The load-bearing assertion of this whole family: the findings a
    label produces must not move because someone printed an instruction on
    it."""

    @pytest.mark.parametrize("payload", _OCR_PAYLOADS, ids=lambda p: p.id)
    def test_an_injected_label_produces_identical_findings_to_a_clean_one(
        self, db, payload
    ) -> None:
        clean = run_adversarial(db, adversarial_case(case_id="injection-baseline"))
        # The payload is printed on the packet as one more real OCR token.
        injected = run_adversarial(
            db,
            adversarial_case(
                case_id=f"injection-{payload.id}",
                tokens=(*BASELINE_TOKENS, payload.text),
            ),
        )

        assert injected.actual_findings == clean.actual_findings, (
            f"payload {payload.id!r} changed the verdicts"
        )
        assert injected.confidence_tier == clean.confidence_tier

    @pytest.mark.parametrize("payload", _OCR_PAYLOADS, ids=lambda p: p.id)
    def test_an_injected_value_is_only_ever_promoted_if_evidence_really_backs_it(
        self, db, payload
    ) -> None:
        """The other half: a *fully compromised* model asserts the
        attacker's text as a real field value while citing the real "250 g"
        token. The invariant is not "every injected value is rejected" - it
        is the stricter, more precise one the P3-T6 gate actually
        guarantees: a value is promoted **iff** the cited evidence supports
        it, by the gate's own published rule (`partial_ratio >=
        MATCH_THRESHOLD`). One corpus payload (`rtl-override`) legitimately
        embeds the real "250 g" text, so the gate verifying it is correct
        behaviour rather than a miss - and the verdict is still unchanged,
        which is what the family actually promises.
        """
        from app.extraction.evidence import MATCH_THRESHOLD, partial_ratio
        from tests.security.adversarial.rig import baseline_extraction

        cited_token_text = "250 g"
        extraction = baseline_extraction()
        extraction["quantity_net_quantity"] = {
            "value": payload.text,
            "not_found_reason": None,
            "token_ids": [1],  # the real "250 g" token
            "confidence": 0.99,
        }

        result = run_adversarial(
            db,
            adversarial_case(case_id=f"injection-promote-{payload.id}", extraction=extraction),
        )

        # The raw claim always survives on the row (P3-T6 rewrites the
        # derived fact, never the `ExtractedField`), which is what the
        # hallucination-rate metric reads.
        assert result.actual_fields["quantity.net_quantity"] == payload.text

        evidence_supports_it = (
            partial_ratio(payload.text, cited_token_text) >= MATCH_THRESHOLD
        )
        assert result.verified_by_field["quantity.net_quantity"] is evidence_supports_it

        if not evidence_supports_it:
            # Demoted: the rule engine never saw the injected value at all.
            assert result.actual_findings["IN-FSSAI-FOOD-NET-QUANTITY-DECLARED"] == (
                "insufficient_data"
            )
            assert result.confidence_tier != "high"
        else:
            # Verified because the text genuinely contains the cited
            # evidence - and the verdict is still a plain `pass`, with no
            # trace of the instruction the attacker smuggled alongside it.
            assert result.actual_findings["IN-FSSAI-FOOD-NET-QUANTITY-DECLARED"] == "pass"


class TestNoChannelForAnInjectionToAct:
    """"no tool/network action": asserted structurally rather than by
    watching for an action that could never be expressed in the first
    place."""

    def test_the_provider_call_is_given_no_tools(self) -> None:
        import inspect

        from app.extraction.llm import base as provider_base

        signature = inspect.signature(provider_base.ExtractionProvider.generate)
        # system_instruction / prompt / schema / model - and nothing that
        # could carry a tool, function, or callback definition.
        assert set(signature.parameters) - {"self"} == {
            "system_instruction",
            "prompt",
            "schema",
            "model",
        }

    def test_the_output_schema_cannot_express_a_verdict_or_an_action(self) -> None:
        schema = json.dumps(ExtractionEnvelope.model_json_schema()).lower()
        for forbidden in ("compliant", "verdict", "approve", "tool", "url", "http"):
            assert forbidden not in schema, f"schema exposes a {forbidden!r}-shaped field"

    @pytest.mark.parametrize("payload", _OCR_PAYLOADS, ids=lambda p: p.id)
    def test_every_corpus_payload_stays_inside_the_untrusted_region(self, payload) -> None:
        rendered = prompt_module.build_user_prompt(
            [
                prompt_module.RenderedToken(token_id=0, text="Ingredients:"),
                prompt_module.RenderedToken(token_id=1, text=payload.text),
            ]
        )
        assert rendered.count(prompt_module.DELIMITER_OPEN) == 1
        assert rendered.count(prompt_module.DELIMITER_CLOSE) == 1
        before, rest = rendered.split(prompt_module.DELIMITER_OPEN, 1)
        _body, after = rest.split(prompt_module.DELIMITER_CLOSE, 1)
        assert payload.text not in before
        assert payload.text not in after
