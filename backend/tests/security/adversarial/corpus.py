"""Loader for the versioned injection corpus (`corpus/injection.json`).

Kept as a tiny module rather than inlined into any one test file because
both the adversarial suite and `tests/unit/test_extraction_prompt.py` load
the same corpus - one corpus, no drift between the prompt-level and
pipeline-level injection tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_CORPUS_PATH = Path(__file__).resolve().parent / "corpus" / "injection.json"


@dataclass(frozen=True, slots=True)
class InjectionPayload:
    id: str
    family: str
    surface: str
    text: str
    note: str


@lru_cache(maxsize=1)
def load_injection_corpus() -> tuple[str, tuple[InjectionPayload, ...]]:
    """`(corpus_version, payloads)` - the version is asserted on in the
    suite so a silently-emptied or swapped corpus fails loudly."""
    raw = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    payloads = tuple(
        InjectionPayload(
            id=entry["id"],
            family=entry["family"],
            surface=entry["surface"],
            text=entry["text"],
            note=entry["note"],
        )
        for entry in raw["payloads"]
    )
    return raw["corpus_version"], payloads


def payloads_for_surface(surface: str) -> tuple[InjectionPayload, ...]:
    _version, payloads = load_injection_corpus()
    return tuple(p for p in payloads if p.surface == surface)
