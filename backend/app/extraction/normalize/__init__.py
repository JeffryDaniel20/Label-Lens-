"""Deterministic normalization library (P3-T7).

Units, numbers, dates, allergen synonyms, ingredient parsing, and language
detection - the step between raw OCR/LLM-extracted strings and the typed
`app.extraction.facts.LabelFacts` the rule engine will eventually consume.
Every function here is pure and takes no ambient state (no clock, no
locale/OS environment, no network, no randomness): the same input always
normalizes to the same output. That determinism is what makes
`NORMALIZER_VERSION` meaningful to pin in a model manifest - re-running an
old analysis against the same recorded version reproduces byte-identical
normalized facts.
"""

from __future__ import annotations

NORMALIZER_VERSION: str = "1.0.0"
