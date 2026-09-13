"""Section 23's "File attacks", "Tenancy", and "Load/abuse" families.

File attacks - *rejected at ingestion with a clear error.*
Tenancy - *404 / denied, audited.*
Load/abuse - *quotas, idempotency dedup, 429s.*

These three families are the ones this codebase already had the most real
coverage for before P7-T4, so this module deliberately does **not**
re-test what is already proven elsewhere:

* `tests/unit/test_ingestion.py` - magic-byte sniffing, SVG-disguised-as-PNG,
  a GIF/PDF polyglot, encrypted/malformed/over-cap PDFs.
* `tests/integration/test_ingestion_upload_completion.py::TestAdversarialUploads`
  - the same rejections over real HTTP against real object storage
  (`object_storage`-marked).
* `tests/security/test_tenant_isolation.py` - every endpoint with a foreign
  id, RBAC denial, CSRF, login enumeration.

What is here is the remainder section 23 names and nothing above covered:
a JPEG/PDF polyglot, an EXIF-payload image, the corpus's filename and
PDF-metadata injection surfaces, and identical-submission dedup. All of it
runs offline, so this suite can be a required CI check without needing
MinIO.
"""

from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.ingestion.magic_bytes import content_type_matches_extension, sniff_mime
from app.ingestion.pdf_checks import validate_pdf
from app.storage.keys import build_object_key, key_belongs_to_org
from tests.conftest import ApiSession
from tests.security.adversarial.corpus import payloads_for_surface

pytestmark = [pytest.mark.security, pytest.mark.integration]

SIGNUP = {
    "organization_name": "Adversarial Co",
    "email": "owner@adversarialco.com",
    "password": "CorrectHorse42!",
}


def _jpeg_bytes(**save_kwargs) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), color=(120, 40, 200)).save(buffer, format="JPEG", **save_kwargs)
    return buffer.getvalue()


def _pdf_bytes(pages: int = 1, **metadata: str) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if metadata:
        writer.add_metadata(metadata)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestFileAttacks:
    def test_a_jpeg_pdf_polyglot_is_sniffed_as_exactly_one_type(self) -> None:
        """A file that is a valid JPEG *and* carries a PDF body: sniffing
        must commit to one type from the leading signature rather than
        accepting whichever the attacker's extension claims."""
        polyglot = _jpeg_bytes() + _pdf_bytes()

        sniffed = sniff_mime(polyglot)

        assert sniffed == "image/jpeg"
        # And the lie in the extension is caught by the cross-check.
        assert content_type_matches_extension(sniffed, "pdf") is False
        assert content_type_matches_extension(sniffed, "jpg") is True

    def test_the_polyglot_is_stopped_by_the_sniff_layer_not_the_pdf_parser(self) -> None:
        """Worth stating precisely, because it is load-bearing and not
        obvious: `validate_pdf` on its own **accepts** a JPEG-prefixed
        polyglot - pypdf tolerates the junk preamble, finds the real
        trailer, and reports a valid one-page document. The rejection comes
        from the layer above it, where the sniffed type (`image/jpeg`) is
        cross-checked against the claimed `.pdf` extension.

        Verified here rather than assumed, so that if anyone ever reorders
        ingestion to parse before sniffing, this test fails loudly instead
        of the polyglot silently becoming acceptable.
        """
        polyglot = _jpeg_bytes() + _pdf_bytes()

        parsed_anyway = validate_pdf(polyglot, max_pages=50)
        assert parsed_anyway.ok is True, (
            "if pypdf ever starts rejecting this, the comment above is stale"
        )

        # The real defence: the bytes are a JPEG, so a `.pdf` claim loses.
        assert content_type_matches_extension(sniff_mime(polyglot), "pdf") is False

    def test_an_exif_payload_does_not_change_how_the_image_is_classified(self) -> None:
        """An EXIF comment carrying an injection payload is inert metadata:
        it must not change the sniffed type, and must not be mistaken for
        content."""
        payload = payloads_for_surface("filename")[0].text.encode()
        exif = Image.Exif()
        exif[0x9286] = payload  # UserComment
        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), color=(10, 10, 10)).save(
            buffer, format="JPEG", exif=exif
        )
        with_exif = buffer.getvalue()

        assert sniff_mime(with_exif) == "image/jpeg"
        assert payload in with_exif  # the payload really is in the file
        # ...and it is still just a JPEG as far as ingestion is concerned.
        assert content_type_matches_extension(sniff_mime(with_exif), "jpg") is True

    def test_pdf_metadata_injection_does_not_change_validation(self) -> None:
        """Section 23's "injection in ... PDF metadata": a Title carrying
        an instruction must not alter the page count, the accept/reject
        decision, or anything else - PDF metadata is never a source of OCR
        tokens, so it can never reach the extraction prompt at all."""
        payload = payloads_for_surface("pdf_metadata")[0].text
        clean = validate_pdf(_pdf_bytes(pages=3), max_pages=50)
        injected = validate_pdf(_pdf_bytes(pages=3, **{"/Title": payload}), max_pages=50)

        assert injected.ok is clean.ok is True
        assert injected.page_count == clean.page_count == 3

    def test_a_filename_injection_payload_cannot_escape_its_storage_prefix(self) -> None:
        """The filename surface: the payload is stored as data, and the key
        derived from it stays inside the org's own prefix. Traversal and
        prefix-escape are what actually matter here - the words in the name
        are inert."""
        payload = payloads_for_surface("filename")[0]
        org_id, version_id = uuid.uuid4(), uuid.uuid4()

        key = build_object_key(
            organization_id=org_id, product_version_id=version_id, filename=payload.text
        )

        assert key_belongs_to_org(key, org_id) is True
        assert key_belongs_to_org(key, uuid.uuid4()) is False
        assert ".." not in key

    @pytest.mark.parametrize(
        "filename",
        ["../../etc/passwd.png", "..\\..\\windows\\system32.png", "a/../../b.png"],
    )
    def test_a_traversal_filename_cannot_escape_its_storage_prefix(self, filename: str) -> None:
        org_id = uuid.uuid4()
        key = build_object_key(
            organization_id=org_id, product_version_id=uuid.uuid4(), filename=filename
        )
        assert key_belongs_to_org(key, org_id) is True
        assert ".." not in key


class TestLoadAndAbuse:
    @pytest.fixture
    def owner(self, client) -> ApiSession:
        client.post("/v1/auth/signup", json=SIGNUP)
        return ApiSession(client, SIGNUP["email"], SIGNUP["password"])

    @pytest.fixture
    def version_with_file(self, owner: ApiSession, db) -> str:
        from app.catalog.models import File, FileStatus

        product_id = owner.post(
            "/v1/products", json={"name": "Masala Chips", "internal_sku": "MC-001"}
        ).json()["id"]
        version_id = owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
        db.add(
            File(
                organization_id=uuid.UUID(owner.org_id),
                product_version_id=uuid.UUID(version_id),
                storage_key=f"org/{owner.org_id}/pv/{version_id}/x.jpg",
                original_filename="label.jpg",
                sha256="a" * 64,
                mime="image/jpeg",
                bytes=123,
                status=FileStatus.READY,
            )
        )
        db.commit()
        return version_id

    def test_repeated_identical_submissions_dedup_instead_of_multiplying_work(
        self, owner: ApiSession, version_with_file: str
    ) -> None:
        """Section 23's "repeated identical submissions": the idempotency
        key is derived from the version + file set, so a burst of identical
        submissions must return the *same* analysis rather than queueing
        twenty of them."""
        responses = [
            owner.post(f"/v1/product-versions/{version_with_file}/analyses", json={})
            for _ in range(5)
        ]

        assert all(r.status_code in (200, 201) for r in responses), [
            r.status_code for r in responses
        ]
        analysis_ids = {r.json()["id"] for r in responses}
        assert len(analysis_ids) == 1, "identical submissions created duplicate analyses"
        # Only the first one is a creation; the rest are recognised as
        # already-existing.
        assert responses[0].status_code == 201
        assert all(r.status_code == 200 for r in responses[1:])

    @pytest.mark.parametrize(
        ("field", "value"),
        [("name", "x" * 5000), ("internal_sku", "y" * 5000)],
    )
    def test_an_oversized_field_is_rejected_not_stored(
        self, owner: ApiSession, field: str, value: str
    ) -> None:
        payload = {"name": "Normal", "internal_sku": "SKU-1"} | {field: value}

        response = owner.post("/v1/products", json=payload)

        # 400 with an RFC 9457 problem document naming the offending field
        # (this app maps validation failures itself rather than returning
        # FastAPI's default 422 - see `app.platform.errors`).
        assert response.status_code == 400, response.text
        body = response.json()
        assert body["type"].endswith("/validation_error")
        assert any(error["field"] == f"body.{field}" for error in body["errors"])
