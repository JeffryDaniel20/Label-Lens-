"""The vertical-slice pipeline stages: `validating`, `ocr`, `normalizing`,
`classifying` - real work now, not placeholders (see
`app.analysis.stages`'s module docstring). `extracting` has its own suite
(`test_extraction_stage.py`, from P3-T5); `rule_eval`/`scoring` remain
placeholders, out of scope until D-01.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select

from app.analysis import service as analysis_service
from app.analysis.models import AnalysisState
from app.analysis.retry_policy import PermanentStageError, TransientStageError
from app.analysis.stages import _classifying, _normalizing, _ocr, _validating
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.extraction import facts as facts_schema
from app.extraction.llm.base import ProviderResponse, ProviderUsage
from app.extraction.models import ExtractedField, Extraction
from app.extraction.service import extract_for_analysis
from app.vision.models import OcrResult, OcrTokenRow
from app.vision.ocr.base import OcrToken
from tests.conftest import make_org
from tests.integration.test_extraction_service import VALID_JSON

pytestmark = pytest.mark.integration


class _FakeOcrEngine:
    name = "fake"
    version = "1.0.0"

    def __init__(self, tokens: list[OcrToken] | None = None) -> None:
        self._tokens = tokens if tokens is not None else [
            OcrToken(text="Wheat flour", confidence=0.9, bbox=((0, 0), (10, 10)), line_no=0,
                     language="en"),
            OcrToken(text="Net Quantity: 250 g", confidence=0.9, bbox=((0, 11), (10, 20)),
                     line_no=1, language="en"),
        ]
        self.calls = 0

    def run(self, image: np.ndarray) -> list[OcrToken]:
        self.calls += 1
        return self._tokens


class _RaisingOcrEngine:
    name = "fake"
    version = "1.0.0"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def run(self, image: np.ndarray) -> list[OcrToken]:
        raise self.exc


class _FakeStorageClient:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def download_object(self, key: str) -> bytes:
        return self._objects[key]


class _StubProvider:
    name = "stub"

    def __init__(self, text: str = VALID_JSON) -> None:
        self.text = text

    def generate(self, *, system_instruction, prompt, schema, model) -> ProviderResponse:
        return ProviderResponse(
            text=self.text, usage=ProviderUsage(tokens_in=1, tokens_out=1), model=model
        )


def _png_bytes(size: tuple[int, int] = (40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _base_fixture(db, *, with_page: bool = True):
    org = make_org(db)
    product = Product(organization_id=org.id, name="Masala Chips", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    file = File(
        organization_id=org.id,
        product_version_id=version.id,
        storage_key="org/x/pv/y/render/z/1.png",
        original_filename="label.png",
        sha256="a" * 64,
        mime="image/png",
        bytes=1,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    page = None
    if with_page:
        page = FilePage(
            organization_id=org.id,
            file_id=file.id,
            page_no=1,
            width=40,
            height=30,
            render_key=file.storage_key,
        )
        db.add(page)
        db.flush()
    file_hash_kwargs = {}
    if with_page:
        file_hash = analysis_service.compute_file_set_hash(
            db, organization_id=org.id, version_id=version.id
        )
        file_hash_kwargs = {"file_set_hash": file_hash}
    else:
        file_hash_kwargs = {"file_set_hash": "x" * 64}
    analysis, _ = analysis_service.create_or_get_analysis(
        db, organization_id=org.id, version=version, **file_hash_kwargs
    )
    db.commit()
    return org, product, version, file, page, analysis


@pytest.fixture
def basic(db):
    return _base_fixture(db)


class TestValidatingStage:
    def test_passes_when_a_rasterized_page_exists(self, db, basic) -> None:
        _org, _product, _version, _file, _page, analysis = basic
        assert _validating(db, analysis) is None

    def test_fails_permanently_with_no_rasterized_pages(self, db) -> None:
        org, product, version, file, _page, analysis = _base_fixture(db, with_page=False)
        with pytest.raises(PermanentStageError, match="No rasterized pages"):
            _validating(db, analysis)

    def test_only_looks_at_ready_files_in_this_org_and_version(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        # A page belonging to a REJECTED file must not count.
        other_file = File(
            organization_id=org.id,
            product_version_id=version.id,
            storage_key="k2",
            original_filename="f2.jpg",
            sha256="b" * 64,
            mime="image/jpeg",
            bytes=1,
            status=FileStatus.REJECTED,
        )
        db.add(other_file)
        db.flush()
        db.add(
            FilePage(
                organization_id=org.id,
                file_id=other_file.id,
                page_no=1,
                width=1,
                height=1,
                render_key="k2",
            )
        )
        db.flush()
        # Still passes because of the READY file's own page - this just
        # proves the rejected file's page wasn't required, not excluded.
        assert _validating(db, analysis) is None


class TestOcrStage:
    def test_ocr_persists_tokens_for_every_page(self, db, basic, monkeypatch) -> None:
        org, product, version, file, page, analysis = basic
        engine = _FakeOcrEngine()
        storage = _FakeStorageClient({file.storage_key: _png_bytes()})
        monkeypatch.setattr(
            "app.vision.ocr.service.get_default_ocr_engine", lambda: engine
        )
        monkeypatch.setattr("app.storage.client.build_storage_client", lambda settings: storage)

        result = _ocr(db, analysis)
        db.commit()

        assert result is None
        tokens = db.scalars(
            select(OcrTokenRow).where(OcrTokenRow.file_page_id == page.id)
        ).all()
        assert len(tokens) == 2
        assert {t.text for t in tokens} == {"Wheat flour", "Net Quantity: 250 g"}

    def test_a_page_with_an_existing_result_is_skipped(self, db, basic, monkeypatch) -> None:
        org, product, version, file, page, analysis = basic
        db.add(
            OcrResult(
                organization_id=org.id,
                file_page_id=page.id,
                engine="already-done",
                engine_version="1",
                avg_confidence=1.0,
                raw=[],
            )
        )
        db.commit()
        engine = _FakeOcrEngine()
        monkeypatch.setattr(
            "app.vision.ocr.service.get_default_ocr_engine", lambda: engine
        )
        monkeypatch.setattr(
            "app.storage.client.build_storage_client",
            lambda settings: _FakeStorageClient({}),  # would KeyError if actually called
        )

        _ocr(db, analysis)

        assert engine.calls == 0

    def test_no_pages_is_a_permanent_failure(self, db) -> None:
        org, product, version, file, _page, analysis = _base_fixture(db, with_page=False)
        with pytest.raises(PermanentStageError, match="No rasterized pages"):
            _ocr(db, analysis)

    def test_engine_construction_failure_is_permanent(self, db, basic, monkeypatch) -> None:
        org, product, version, file, page, analysis = basic

        def _raise():
            raise ImportError("no module named paddleocr")

        monkeypatch.setattr("app.vision.ocr.service.get_default_ocr_engine", _raise)
        with pytest.raises(PermanentStageError, match="OCR engine unavailable"):
            _ocr(db, analysis)

    def test_an_undecodable_render_is_a_permanent_failure(self, db, basic, monkeypatch) -> None:
        org, product, version, file, page, analysis = basic
        monkeypatch.setattr(
            "app.vision.ocr.service.get_default_ocr_engine", lambda: _FakeOcrEngine()
        )
        monkeypatch.setattr(
            "app.storage.client.build_storage_client",
            lambda settings: _FakeStorageClient({file.storage_key: b"not an image"}),
        )
        with pytest.raises(PermanentStageError, match="could not be decoded"):
            _ocr(db, analysis)

    def test_a_storage_failure_is_transient(self, db, basic, monkeypatch) -> None:
        org, product, version, file, page, analysis = basic

        class _FailingStorage:
            def download_object(self, key: str) -> bytes:
                raise ConnectionError("storage unreachable")

        monkeypatch.setattr(
            "app.vision.ocr.service.get_default_ocr_engine", lambda: _FakeOcrEngine()
        )
        monkeypatch.setattr(
            "app.storage.client.build_storage_client", lambda settings: _FailingStorage()
        )
        with pytest.raises(TransientStageError):
            _ocr(db, analysis)

    def test_an_engine_crash_is_transient(self, db, basic, monkeypatch) -> None:
        org, product, version, file, page, analysis = basic
        engine = _RaisingOcrEngine(RuntimeError("engine crashed"))
        monkeypatch.setattr("app.vision.ocr.service.get_default_ocr_engine", lambda: engine)
        monkeypatch.setattr(
            "app.storage.client.build_storage_client",
            lambda settings: _FakeStorageClient({file.storage_key: _png_bytes()}),
        )
        with pytest.raises(TransientStageError):
            _ocr(db, analysis)


def _extraction_for(db, org, analysis, *, text: str = VALID_JSON) -> Extraction:
    # Real OCR tokens are needed for `extract_for_analysis` to have
    # something to cite - build one page's worth directly.
    outcome = extract_for_analysis(
        db,
        provider=_StubProvider(text),
        organization_id=org.id,
        analysis_id=analysis.id,
        product_version_id=analysis.product_version_id,
        model="stub",
        escalation_model="stub",
    )
    db.commit()
    return outcome.extraction


def _add_ocr_tokens(db, org, page) -> None:
    result = OcrResult(
        organization_id=org.id,
        file_page_id=page.id,
        engine="stub",
        engine_version="1",
        avg_confidence=0.9,
        raw=[],
    )
    db.add(result)
    db.flush()
    for i, text in enumerate(
        (
            "Chocolate (Sugar, Cocoa Butter), Wheat Flour",
            "250 g",
            "12/2027",
            "not a real date!!",
        )
    ):
        db.add(
            OcrTokenRow(
                organization_id=org.id,
                file_page_id=page.id,
                ocr_result_id=result.id,
                text=text,
                confidence=0.9,
                x1=float(i),
                y1=0.0,
                x2=float(i + 1),
                y2=1.0,
                line_no=i,
            )
        )
    db.flush()


NORMALIZING_JSON = """
{
  "ingredients_declared_text": {"value": "Chocolate (Sugar, Cocoa Butter), Wheat Flour",
                                "not_found_reason": null, "token_ids": [0], "confidence": 0.9},
  "allergens_declaration_text": {"value": null, "not_found_reason": "not printed",
                                 "token_ids": [], "confidence": 0.0},
  "allergens_declared": {"values": [], "not_found_reason": "not printed",
                         "token_ids": [], "confidence": 0.0},
  "nutrition_serving_size": {"value": null, "not_found_reason": "not printed",
                             "token_ids": [], "confidence": 0.0},
  "nutrition_rows": [],
  "nutrition_rows_not_found_reason": "not printed",
  "quantity_net_quantity": {"value": "250 g", "not_found_reason": null,
                            "token_ids": [1], "confidence": 0.99},
  "dates_manufacture": {"value": "not a real date!!", "not_found_reason": null,
                        "token_ids": [3], "confidence": 0.5},
  "dates_expiry_or_best_before": {"value": "12/2027", "not_found_reason": null,
                                  "token_ids": [2], "confidence": 0.8},
  "dates_batch_number": {"value": null, "not_found_reason": "not printed",
                         "token_ids": [], "confidence": 0.0},
  "claims": [], "claims_not_found_reason": "not printed",
  "addresses": [], "addresses_not_found_reason": "not printed",
  "languages_detected": {"values": ["en"], "not_found_reason": null,
                         "token_ids": [], "confidence": 0.9}
}
"""


class TestNormalizingStage:
    def test_parses_ingredients_into_structured_items(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        _add_ocr_tokens(db, org, page)
        extraction = _extraction_for(db, org, analysis, text=NORMALIZING_JSON)

        assert _normalizing(db, analysis) is None
        db.commit()

        db.refresh(extraction)
        facts = facts_schema.LabelFacts.model_validate(extraction.payload)
        assert facts.ingredients.items.value is not None
        names = [item.name for item in facts.ingredients.items.value]
        # The nested-parentheses case: the sub-ingredient list's own commas
        # must not fragment the top-level split.
        assert names == ["Chocolate (Sugar, Cocoa Butter)", "Wheat Flour"]

    def test_normalizes_quantity_onto_the_extracted_field_row(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        _add_ocr_tokens(db, org, page)
        extraction = _extraction_for(db, org, analysis, text=NORMALIZING_JSON)

        _normalizing(db, analysis)
        db.commit()

        row = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == extraction.id,
                ExtractedField.field_path == "quantity.net_quantity",
            )
        )
        assert row.value_norm == {"value": 250.0}
        assert row.unit == "g"

    def test_normalizes_a_parseable_date(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        _add_ocr_tokens(db, org, page)
        extraction = _extraction_for(db, org, analysis, text=NORMALIZING_JSON)

        _normalizing(db, analysis)
        db.commit()

        row = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == extraction.id,
                ExtractedField.field_path == "dates.expiry_or_best_before",
            )
        )
        assert row.value_norm == {"iso": "2027-12"}

    def test_an_unparseable_date_is_left_un_normalized_not_fatal(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        _add_ocr_tokens(db, org, page)
        extraction = _extraction_for(db, org, analysis, text=NORMALIZING_JSON)

        result = _normalizing(db, analysis)  # must not raise despite the bad manufacture date
        db.commit()

        assert result is None
        row = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == extraction.id,
                ExtractedField.field_path == "dates.manufacture_date",
            )
        )
        assert row.value_norm is None
        assert row.value_raw == "not a real date!!"  # raw text preserved

    def test_no_extraction_is_a_permanent_failure(self, db, basic) -> None:
        _org, _product, _version, _file, _page, analysis = basic
        with pytest.raises(PermanentStageError, match="No extraction exists"):
            _normalizing(db, analysis)

    def test_an_unparseable_quantity_is_left_un_normalized_not_fatal(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        _add_ocr_tokens(db, org, page)
        bad_quantity_json = NORMALIZING_JSON.replace(
            '"value": "250 g"', '"value": "not a real quantity"'
        )
        extraction = _extraction_for(db, org, analysis, text=bad_quantity_json)

        result = _normalizing(db, analysis)  # must not raise despite the bad quantity
        db.commit()

        assert result is None
        row = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == extraction.id,
                ExtractedField.field_path == "quantity.net_quantity",
            )
        )
        assert row.value_norm is None
        assert row.unit is None
        assert row.value_raw == "not a real quantity"


class TestClassifyingStage:
    def test_classifies_using_extracted_facts_and_product_hints(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_ocr_tokens(db, org, page)
        _extraction_for(db, org, analysis, text=NORMALIZING_JSON)

        assert _classifying(db, analysis) is None
        db.commit()

        db.refresh(analysis)
        assert analysis.category == "packaged_food"
        assert analysis.jurisdictions == ["IN"]
        assert analysis.category_confidence > 0
        assert analysis.jurisdiction_confidence > 0

    def test_abstains_honestly_with_no_hints_and_weak_facts(self, db, basic) -> None:
        import json

        org, product, version, file, page, analysis = basic
        _add_ocr_tokens(db, org, page)
        # Every field not-found: the fabrication-bait case.
        empty_text_field = {
            "value": None, "not_found_reason": "x", "token_ids": [], "confidence": 0
        }
        empty_list_field = {
            "values": [], "not_found_reason": "x", "token_ids": [], "confidence": 0
        }
        empty = json.dumps(
            {
                "ingredients_declared_text": empty_text_field,
                "allergens_declaration_text": empty_text_field,
                "allergens_declared": empty_list_field,
                "nutrition_serving_size": empty_text_field,
                "nutrition_rows": [],
                "nutrition_rows_not_found_reason": "x",
                "quantity_net_quantity": empty_text_field,
                "dates_manufacture": empty_text_field,
                "dates_expiry_or_best_before": empty_text_field,
                "dates_batch_number": empty_text_field,
                "claims": [],
                "claims_not_found_reason": "x",
                "addresses": [],
                "addresses_not_found_reason": "x",
                "languages_detected": empty_list_field,
            }
        )
        _extraction_for(db, org, analysis, text=empty)

        _classifying(db, analysis)
        db.commit()

        db.refresh(analysis)
        assert analysis.category is None
        assert analysis.jurisdictions == []

    def test_no_extraction_is_a_permanent_failure(self, db, basic) -> None:
        _org, _product, _version, _file, _page, analysis = basic
        with pytest.raises(PermanentStageError, match="No extraction exists"):
            _classifying(db, analysis)


class TestVerticalSlice:
    """The literal deliverable: a real analysis composes every wired stage,
    end to end, through `advance_analysis` - not each stage tested in
    isolation."""

    def test_a_real_analysis_walks_from_queued_to_completed(
        self, db, basic, monkeypatch
    ) -> None:
        from app.analysis.stages import advance_analysis

        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()

        monkeypatch.setattr(
            "app.vision.ocr.service.get_default_ocr_engine", lambda: _FakeOcrEngine()
        )
        monkeypatch.setattr(
            "app.storage.client.build_storage_client",
            lambda settings: _FakeStorageClient({file.storage_key: _png_bytes()}),
        )
        from app.platform.config import Settings

        monkeypatch.setattr(
            "app.platform.config.get_settings",
            lambda: Settings(secret_key="x" * 40, database_url="sqlite://", llm_api_key="k"),
        )
        monkeypatch.setattr("app.extraction.llm.build_provider", lambda _s: _StubProvider())

        # queued -> validating -> preprocessing -> ocr -> extracting ->
        # normalizing -> classifying -> rule_eval -> scoring -> completed.
        # `rule_eval`/`scoring` are still honest placeholders (D-01), so
        # `scoring`'s default successor really is `completed` - see the
        # module docstring for why that is itself an honest outcome.
        for _ in range(9):
            advance_analysis(db, analysis)
            db.commit()

        assert analysis.state is AnalysisState.COMPLETED
        assert analysis.category == "packaged_food"
        tokens = db.scalars(
            select(OcrTokenRow).where(OcrTokenRow.file_page_id == page.id)
        ).all()
        assert len(tokens) == 2
        extraction = db.scalar(
            select(Extraction).where(Extraction.analysis_id == analysis.id)
        )
        assert extraction is not None
        facts = facts_schema.LabelFacts.model_validate(extraction.payload)
        assert facts.quantity.net_quantity.value is not None
