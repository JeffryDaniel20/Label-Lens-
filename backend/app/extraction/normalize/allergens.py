"""Allergen synonym dictionary and canonicalization.

Covers the EU's 14 legally-declarable allergens (Regulation (EU) No
1169/2011, Annex II) and FSSAI's major-allergen list (FSS (Labelling &
Display) Regulations, 2020) - the two jurisdictions this project currently
targets (see D-01). Synonyms are the common forms these actually take on
real ingredient declarations (a sub-ingredient name, a regional term), not
an exhaustive linguistic corpus; extending the dictionary as real labels
surface gaps is expected routine maintenance, tracked by bumping
`NORMALIZER_VERSION`.
"""

from __future__ import annotations

CANONICAL_ALLERGENS: frozenset[str] = frozenset(
    {
        "milk",
        "eggs",
        "fish",
        "crustaceans",
        "molluscs",
        "tree_nuts",
        "peanuts",
        "wheat",
        "soybeans",
        "sesame",
        "celery",
        "mustard",
        "sulphites",
        "lupin",
    }
)

ALLERGEN_SYNONYMS: dict[str, str] = {
    "milk": "milk",
    "dairy": "milk",
    "milk solids": "milk",
    "milk powder": "milk",
    "skimmed milk powder": "milk",
    "whole milk powder": "milk",
    "butter": "milk",
    "ghee": "milk",
    "casein": "milk",
    "whey": "milk",
    "lactose": "milk",
    "egg": "eggs",
    "eggs": "eggs",
    "egg powder": "eggs",
    "albumin": "eggs",
    "fish": "fish",
    "anchovy": "fish",
    "anchovies": "fish",
    "fish sauce": "fish",
    "prawn": "crustaceans",
    "prawns": "crustaceans",
    "shrimp": "crustaceans",
    "shrimps": "crustaceans",
    "crab": "crustaceans",
    "lobster": "crustaceans",
    "crustaceans": "crustaceans",
    "mussel": "molluscs",
    "mussels": "molluscs",
    "squid": "molluscs",
    "oyster": "molluscs",
    "oysters": "molluscs",
    "molluscs": "molluscs",
    "almond": "tree_nuts",
    "almonds": "tree_nuts",
    "cashew": "tree_nuts",
    "cashews": "tree_nuts",
    "walnut": "tree_nuts",
    "walnuts": "tree_nuts",
    "pistachio": "tree_nuts",
    "pistachios": "tree_nuts",
    "hazelnut": "tree_nuts",
    "hazelnuts": "tree_nuts",
    "tree nuts": "tree_nuts",
    "tree_nuts": "tree_nuts",
    "peanut": "peanuts",
    "peanuts": "peanuts",
    "groundnut": "peanuts",
    "groundnuts": "peanuts",
    "wheat": "wheat",
    "wheat flour": "wheat",
    "maida": "wheat",
    "atta": "wheat",
    "semolina": "wheat",
    "gluten": "wheat",
    "soy": "soybeans",
    "soya": "soybeans",
    "soybean": "soybeans",
    "soybeans": "soybeans",
    "soy lecithin": "soybeans",
    "sesame": "sesame",
    "sesame seed": "sesame",
    "sesame seeds": "sesame",
    "til": "sesame",
    "celery": "celery",
    "mustard": "mustard",
    "mustard seed": "mustard",
    "sulphite": "sulphites",
    "sulphites": "sulphites",
    "sulfite": "sulphites",
    "sulfites": "sulphites",
    "lupin": "lupin",
    "lupine": "lupin",
}


def canonicalize_allergen(text: str) -> str | None:
    """The canonical allergen key for `text`, or `None` if not recognized."""
    return ALLERGEN_SYNONYMS.get(text.strip().lower())


def is_allergen(text: str) -> bool:
    return canonicalize_allergen(text) is not None
