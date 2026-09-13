"""The adversarial suite (P7-T4): IMPLEMENTATION.md section 23's nine
families, automated.

One module per family, each asserting that family's own stated expected
behaviour end to end - not the primitives underneath it. Where a primitive
already has dedicated unit coverage (prompt framing in
`tests/unit/test_extraction_prompt.py`, magic-byte/PDF validation in
`tests/unit/test_ingestion.py`), the family module here asserts the
*behaviour that those primitives are supposed to produce* rather than
re-testing them, so a passing unit test can never mask a broken pipeline.

`corpus/injection.json` is the versioned injection corpus section 23 calls
for, shared with the prompt unit tests so there is exactly one corpus to
maintain.
"""

from __future__ import annotations
