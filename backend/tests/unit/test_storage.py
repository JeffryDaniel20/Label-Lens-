"""Unit tests for the object storage key scheme and ownership checks."""

from __future__ import annotations

import uuid

import pytest

from app.catalog.models import ProductVersion
from app.platform.errors import NotFound
from app.storage.keys import (
    build_object_key,
    extract_extension,
    key_belongs_to_org,
    key_product_version_id,
)
from app.storage.service import request_download, request_upload

pytestmark = pytest.mark.unit


class TestExtension:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("label.jpg", "jpg"),
            ("label.PNG", "png"),
            ("scan.PDF", "pdf"),
            ("archive.tar.gz", "bin"),
            ("no-extension", "bin"),
            ("evil.exe", "bin"),
            ("sneaky.jpg.exe", "bin"),
        ],
    )
    def test_extension_extraction_and_allowlist(self, filename: str, expected: str) -> None:
        assert extract_extension(filename) == expected


class TestObjectKey:
    def test_key_is_namespaced_by_org_and_version(self) -> None:
        org_id, version_id = uuid.uuid4(), uuid.uuid4()
        key = build_object_key(
            organization_id=org_id, product_version_id=version_id, filename="label.jpg"
        )
        assert key.startswith(f"org/{org_id}/pv/{version_id}/")
        assert key.endswith(".jpg")

    def test_keys_are_unique_per_call(self) -> None:
        org_id, version_id = uuid.uuid4(), uuid.uuid4()
        keys = {
            build_object_key(
                organization_id=org_id, product_version_id=version_id, filename="a.png"
            )
            for _ in range(20)
        }
        assert len(keys) == 20

    def test_key_belongs_to_org_is_exact_and_rejects_traversal(self) -> None:
        org_id = uuid.uuid4()
        other_id = uuid.uuid4()
        key = build_object_key(
            organization_id=org_id, product_version_id=uuid.uuid4(), filename="a.png"
        )
        assert key_belongs_to_org(key, org_id)
        assert not key_belongs_to_org(key, other_id)
        assert not key_belongs_to_org(f"org/{org_id}/../{other_id}/pv/x/y", org_id)
        assert not key_belongs_to_org(f"/org/{org_id}/pv/x/y", org_id)

    def test_key_product_version_id_roundtrips(self) -> None:
        org_id, version_id = uuid.uuid4(), uuid.uuid4()
        key = build_object_key(
            organization_id=org_id, product_version_id=version_id, filename="a.png"
        )
        assert key_product_version_id(key) == version_id

    def test_key_product_version_id_is_none_for_malformed_keys(self) -> None:
        assert key_product_version_id("not-a-key") is None
        assert key_product_version_id("org/x/pv/not-a-uuid/y") is None


class _FakeStorageClient:
    """Records calls instead of talking to S3, for pure service-layer tests."""

    def __init__(self) -> None:
        self.put_calls: list[tuple[str, str, int]] = []
        self.get_calls: list[tuple[str, int]] = []

    def generate_presigned_put(self, key: str, *, content_type: str, expires_in: int) -> str:
        self.put_calls.append((key, content_type, expires_in))
        return f"https://storage.example/{key}?signed=put"

    def generate_presigned_get(self, key: str, *, expires_in: int) -> str:
        self.get_calls.append((key, expires_in))
        return f"https://storage.example/{key}?signed=get"


class TestStorageService:
    def _version(self, org_id: uuid.UUID) -> ProductVersion:
        version = ProductVersion(organization_id=org_id, product_id=uuid.uuid4(), version_no=1)
        version.id = uuid.uuid4()
        return version

    def test_request_upload_issues_a_scoped_key_and_url(self) -> None:
        org_id = uuid.uuid4()
        client = _FakeStorageClient()
        ticket = request_upload(
            client,
            organization_id=org_id,
            version=self._version(org_id),
            filename="label.jpg",
            content_type="image/jpeg",
            ttl_seconds=300,
        )
        assert ticket.key.startswith(f"org/{org_id}/")
        assert ticket.upload_url.endswith("?signed=put")
        assert client.put_calls == [(ticket.key, "image/jpeg", 300)]

    def test_request_upload_rejects_a_version_from_another_org(self) -> None:
        org_id = uuid.uuid4()
        other_org_version = self._version(uuid.uuid4())
        with pytest.raises(NotFound):
            request_upload(
                _FakeStorageClient(),
                organization_id=org_id,
                version=other_org_version,
                filename="label.jpg",
                content_type="image/jpeg",
                ttl_seconds=300,
            )

    def test_request_download_issues_a_url_for_an_owned_key(self) -> None:
        org_id = uuid.uuid4()
        client = _FakeStorageClient()
        key = build_object_key(
            organization_id=org_id, product_version_id=uuid.uuid4(), filename="a.png"
        )
        ticket = request_download(client, organization_id=org_id, key=key, ttl_seconds=120)
        assert ticket.url.endswith("?signed=get")
        assert client.get_calls == [(key, 120)]

    def test_request_download_rejects_a_key_from_another_org(self) -> None:
        org_id = uuid.uuid4()
        foreign_key = build_object_key(
            organization_id=uuid.uuid4(), product_version_id=uuid.uuid4(), filename="a.png"
        )
        with pytest.raises(NotFound):
            request_download(
                _FakeStorageClient(), organization_id=org_id, key=foreign_key, ttl_seconds=120
            )
