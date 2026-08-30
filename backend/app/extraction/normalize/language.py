"""Deterministic language detection for label text blocks.

Uses `py3langid` rather than the more common `langdetect` package:
`langdetect`'s Naive Bayes classifier samples from a PRNG internally and is
not deterministic run-to-run unless the caller remembers to seed it
(`DetectorFactory.seed = 0`), which conflicts outright with this task's
"deterministic" requirement. `py3langid` has no randomness at all - the same
input always produces the same output.

The candidate language set is restricted to languages actually relevant to
this project's target jurisdictions (India/FSSAI plus the EU's major
languages) rather than py3langid's full ~97-language default set. On the
short text blocks a label panel actually produces, an unrestricted
classifier misclassifies far more often - empirically, "Contains: Milk, Soy"
against the full model comes back as Swedish - simply because it has many
more plausible-looking candidates to confuse a handful of words with.

Calling `py3langid.set_languages()` mutates shared module-level state in the
`py3langid` package for the whole process; that is intentional here (this
module always wants the same restricted set) and is done once at import
time, so it stays consistent with "deterministic" rather than undermining it.
"""

from __future__ import annotations

import py3langid as _langid

_SUPPORTED_LANGUAGES: tuple[str, ...] = (
    "en",
    "hi",
    "bn",
    "ta",
    "te",
    "mr",
    "gu",
    "pa",
    "fr",
    "de",
    "es",
    "it",
    "pt",
    "nl",
    "pl",
    "ro",
)
_langid.set_languages(list(_SUPPORTED_LANGUAGES))

# Below this length, language ID on label-panel-sized snippets is unreliable
# enough that returning a confident-looking answer would be worse than
# admitting uncertainty.
_MIN_CHARS_FOR_RELIABLE_DETECTION = 12


def detect_language(text: str) -> str | None:
    """Best-effort ISO 639-1 code for `text`, or `None` if too short to trust."""
    stripped = text.strip()
    if len(stripped) < _MIN_CHARS_FOR_RELIABLE_DETECTION:
        return None
    code, _score = _langid.classify(stripped)
    return str(code)
