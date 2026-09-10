# Rule authoring guide (P4-T6)

How to add a rule to a rule pack (e.g. `backend/app/rulesets/in-fssai-food/v1.0.0/`) without
touching any Python — the scaffolding CLI and every test that exercises a pack's content work
entirely off the pack's own YAML/JSON files on disk.

## The pieces

A pack directory (`{jurisdiction}-{authority}-{category}/{version}/`) has:

```
manifest.yaml           # jurisdiction, category, semver, effective window, citations, author
rules/*.yaml            # one rule per file, loaded in filename order
dictionaries/*.yaml     # documentation only - see "Dictionaries" below
fixtures/<RULE-KEY>/{pass,fail,insufficient_data}.json
```

`app.rules.loader.load_pack_from_directory` reads `manifest.yaml` plus every `rules/*.yaml` and
validates the result strictly — a malformed rule fails to load with a precise error, never
silently. Nothing here is specific to `in-fssai-food`; this is how every pack works.

## Adding a rule

### 1. Find the real citation first

Never write a rule before you have the regulation's exact clause in front of you. `in-fssai-food`'s
own rules quote the official FSSAI compendium PDF verbatim — see `manifest.yaml`'s own
`source_citations` for where that came from. A rule with a paraphrased or invented citation is
worse than no rule at all in a compliance product: fix the source, not the citation, if they
disagree.

### 2. Check what `LabelFacts` actually captures

A rule can only check what the extraction pipeline actually extracts —
`backend/app/extraction/facts.py`'s `LabelFacts` is the complete, frozen list of fields available:

| Field path | Shape |
|---|---|
| `ingredients.declared_text` | text |
| `ingredients.items` | list |
| `allergens.declaration_text` | text |
| `allergens.declared` | list |
| `nutrition.serving_size` | text |
| `nutrition.rows` | list |
| `quantity.net_quantity` | text |
| `dates.manufacture_date` | text |
| `dates.expiry_or_best_before` | text |
| `dates.batch_number` | text |
| `claims.items` | list |
| `addresses.items` | list |
| `languages.detected` | list |

If the declaration you need to check isn't on this list (e.g. FSSAI licence number, veg/non-veg
symbol, country of origin — none of these are extracted today), you cannot write a real rule for it
yet. Extending `LabelFacts` is a separate, larger change (it ripples through OCR, extraction,
normalization, and evidence verification) — don't fabricate a check against data that doesn't
exist; say so in the rule pack's own docs instead, the way `in-fssai-food`'s `manifest.yaml` does.

### 3. Scaffold it

For the common case — "is this field declared" (and, for a text field, "declared and non-blank") —
run:

```bash
cd backend
python scripts/new_rule.py app/rulesets/in-fssai-food/v1.0.0 IN-FSSAI-FOOD-YOUR-RULE-KEY \
    --field dates.batch_number \
    --title "Batch number is declared" \
    --citation "Regulation 5(9): \"A batch number ... shall be declared on the label.\"" \
    --severity major
```

This writes `rules/<NN>_your_rule_key.yaml` (numbered after the last rule already in the pack) and
`fixtures/IN-FSSAI-FOOD-YOUR-RULE-KEY/{pass,fail,insufficient_data}.json` (or just `{pass,
insufficient_data}.json` for a list-shaped field — see "Field kinds" below), and validates the
generated rule against the real `app.rules.schema.Rule` model before writing anything, so it can
never leave a broken file on disk. `rule_key` must be `UPPER-SNAKE-SEGMENTS`; `--field` must be a
dotted path from the table above.

Run `python scripts/new_rule.py --help` for the full flag list (`--kind text|list`,
`--jurisdiction`, `--category`).

### 4. Hand-edit the logic if it's not a plain presence check

The scaffolded `logic:` block is always one of:

```yaml
# text field: declared AND non-blank
logic:
  regex_matches:
    path: dates.batch_number
    pattern: "\\S"
```

```yaml
# list field: declared (only "declared" vs "not declared" is checkable -
# see "Field kinds" below for why)
logic:
  field_present: addresses.items
```

For anything else — "every declared allergen is a recognized name" (`for_each` +
`in_allergen_dictionary`), "at least one of two languages is present" (`any`), a numeric range
(`numeric_within`), a unit check (`unit_convertible_to`) — edit `logic:` directly. The full
predicate registry is `backend/app/rules/predicates.py::PREDICATES`; a rule can only reference a
predicate already in that closed list (P4-T2) — publishing a pack that references anything else
fails at load time with a precise error. See `rules/03_allergen_names_recognized.yaml` in
`in-fssai-food` for a worked `for_each` example, and `rules/11_label_language_compliant.yaml` for a
worked `any` example.

If your rule needs a predicate that needs data external to the facts payload (like
`in_allergen_dictionary` needs the allergen dictionary), the caller — `app.analysis.stages._rule_eval`
— has to pass it via `evaluate()`'s `predicate_kwargs`. Check that function's own `predicate_kwargs`
dict before assuming a new predicate-with-external-data rule will "just work"; if it needs a new
key there, that one line *is* the Python touching this task's acceptance line makes an exception
for — it's wiring, not rule content.

### 5. Regenerate fixtures if you hand-edit `logic:` in a way that changes what should pass

The scaffolded fixtures are generated from the one shared "fully-compliant label" baseline in
`scripts/_fixture_baseline.py` (`BASE_COMPLIANT_FACTS`) — the same baseline `in-fssai-food`'s own
`scripts/_gen_fssai_fixtures.py` uses for every existing rule, so there is exactly one definition of
"what a compliant label looks like" in this codebase, never two that could drift apart. If you
change `logic:` after scaffolding (e.g. add an `any`/`all` combinator), re-derive the fixtures by
hand from that same baseline rather than hand-writing an unrelated label from scratch — pass the
baseline as-is, and use `with_field(BASE_COMPLIANT_FACTS, "your.field", fact(...))` for the
fail/insufficient-data variants (see `_gen_fssai_fixtures.py`'s `CASES` table for the pattern).

### 6. Field kinds: text vs list

- **text** fields (`Fact[str]`) support three genuinely distinct outcomes: `pass` (non-blank),
  `fail` (declared but blank), `insufficient_data` (not declared at all). The scaffolder generates
  all three fixtures for these.
- **list** fields (`Fact[list[...]]`) only support `pass`/`insufficient_data` through
  `field_present` alone — a value that resolves at all is always a non-empty, schema-valid Python
  list, so `field_present` can never see the difference between "declared" and "declared but
  empty." The scaffolder generates only `pass.json` and `insufficient_data.json` for these, and
  adds a note to the rule's own `citation` saying so — state this plainly in any rule you hand-write
  against a list field too, rather than shipping a fabricated `fail` fixture that could never
  actually occur.

### 7. Evidence fields: cite what has a real citation

`evidence.fields` must list a `LabelFacts` path that actually gets its own `ExtractedField`/
`EvidenceSpan` row — check `backend/app/extraction/service.py`'s field-row list before citing a
field. Notably, **`ingredients.items` does not have its own evidence row** — it's a pure
normalization derivative of `ingredients.declared_text` (P3-T7's ingredient splitter), computed
after extraction, with no citation of its own. A rule that cites it in `evidence.fields` will load
and evaluate fine, but crashes with a `RuntimeError` the moment `persist_findings` tries to resolve
real evidence for a real `pass`/`fail` finding (its own structural invariant: a `pass`/`fail`
finding must always resolve to at least one evidence span). If you're checking a field like this,
cite the real underlying field instead (`ingredients.declared_text`, in that case) and say so in the
rule's citation, exactly as `in-fssai-food`'s own `IN-FSSAI-FOOD-INGREDIENTS-ITEMS-ITEMIZED` rule
does.

### 8. Test it

Nothing here needs a new test written by hand for a new rule in an existing pack —
`backend/tests/rules/test_in_fssai_food_pack.py` already discovers every rule and every fixture
file on disk dynamically (it globs `rules/*.yaml` and `fixtures/*/*.json`, it does not hardcode rule
keys), so a new rule's fixtures are automatically replayed through the real evaluator and asserted
against the status their filename promises the moment you add them. Just run:

```bash
cd backend
python -m pytest tests/rules/ -q
```

If you scaffolded into a *new* pack directory rather than an existing one, copy that test file's
pattern (`PACK_DIR`, the fixture-globbing helpers) — pointing it at your new pack directory is the
only change needed.

### 9. Publish it

```bash
cd backend
python scripts/publish_ruleset.py app/rulesets/in-fssai-food/v1.0.0
```

Publishing is idempotent (identical content re-publishes to the same `Ruleset` row) and rejects a
conflicting change to an already-published `(jurisdiction, category, version)` — bump `version` in
`manifest.yaml` for a genuine content change instead of trying to edit a published version in
place. See `app.rules.publish`'s own docstring for the full guarantee.

## Dictionaries

`dictionaries/*.yaml` in a pack directory is documentation only, never loaded by any code path —
see `in-fssai-food/v1.0.0/dictionaries/allergens.yaml`'s own comment for why. The one dictionary a
rule can currently depend on (the allergen synonym table `in_allergen_dictionary` checks against)
lives in exactly one place, `backend/app/extraction/normalize/allergens.py`'s
`ALLERGEN_SYNONYMS` — update that file, not a pack's `dictionaries/` directory, if a real allergen
needs a new synonym recognized. Duplicating that table inside a pack would risk two allergen lists
silently drifting apart, the one hazard `app.rules.predicates`'s own docstring calls out explicitly.
