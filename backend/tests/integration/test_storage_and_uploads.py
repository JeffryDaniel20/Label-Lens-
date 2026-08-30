"""Storage router tests, plus end-to-end presigned-URL tests against a live
S3-compatible endpoint (MinIO), gated by `LABELLENS_TEST_S3_ENDPOINT`.

The router tests (auth, capability, tenant ownership, response shape) run
everywhere: presigned-URL signing is local and needs no network call. The
`object_storage`-marked tests prove the whole point of this design - that a
file uploads directly to the object store without ever passing through the
API - by performing a real HTTP PUT against a real bucket.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from app.storage.client import S3ObjectStorage
from app.storage.keys import build_object_key
from tests.conftest import ApiSession

pytestmark = pytest.mark.integration

SIGNUP = {
    "organization_name": "Acme Foods",
    "email": "owner@acmefoods.com",
    "password": "CorrectHorse42!",
}

S3_ENDPOINT = os.environ.get("LABELLENS_TEST_S3_ENDPOINT")


@pytest.fixture
def owner(client) -> ApiSession:
    client.post("/v1/auth/signup", json=SIGNUP)
    return ApiSession(client, SIGNUP["email"], SIGNUP["password"])


def _product_version(owner: ApiSession) -> str:
    product_id = owner.post(
        "/v1/products", json={"name": "Masala Chips", "internal_sku": "MC-001"}
    ).json()["id"]
    return owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]


class TestUploadRouterAuthAndValidation:
    def test_requesting_an_upload_url_requires_authentication(self, client) -> None:
        response = client.post(
            f"/v1/product-versions/{uuid.uuid4()}/uploads",
            json={"filename": "label.jpg", "content_type": "image/jpeg"},
        )
        assert response.status_code == 401

    def test_viewer_cannot_request_an_upload_url(self, client) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        owner = ApiSession(client, SIGNUP["email"], SIGNUP["password"])
        owner.post(
            "/v1/members",
            json={
                "email": "viewer@acmefoods.com",
                "role": "viewer",
                "password": "CorrectHorse42!",
            },
        )
        viewer = ApiSession(client, "viewer@acmefoods.com")
        version_id = _product_version(owner)
        response = viewer.post(
            f"/v1/product-versions/{version_id}/uploads",
            json={"filename": "label.jpg", "content_type": "image/jpeg"},
        )
        assert response.status_code == 403

    def test_analyst_receives_a_scoped_upload_url(self, owner: ApiSession) -> None:
        version_id = _product_version(owner)
        response = owner.post(
            f"/v1/product-versions/{version_id}/uploads",
            json={"filename": "label.jpg", "content_type": "image/jpeg"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["key"].startswith(f"org/{owner.org_id}/pv/{version_id}/")
        assert body["expires_in"] > 0
        assert "signature" in body["upload_url"].lower() or "x-amz" in body["upload_url"].lower()

    def test_disallowed_file_type_is_rejected(self, owner: ApiSession) -> None:
        version_id = _product_version(owner)
        response = owner.post(
            f"/v1/product-versions/{version_id}/uploads",
            json={"filename": "malware.exe", "content_type": "application/octet-stream"},
        )
        assert response.status_code == 400

    def test_mismatched_content_type_for_extension_is_rejected(self, owner: ApiSession) -> None:
        version_id = _product_version(owner)
        response = owner.post(
            f"/v1/product-versions/{version_id}/uploads",
            json={"filename": "label.jpg", "content_type": "application/pdf"},
        )
        assert response.status_code == 400

    def test_upload_url_for_another_orgs_version_is_404(self, client) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        client.post(
            "/v1/auth/signup",
            json={
                "organization_name": "Beta Foods",
                "email": "owner@betafoods.com",
                "password": "CorrectHorse42!",
            },
        )
        a = ApiSession(client, SIGNUP["email"], SIGNUP["password"])
        b = ApiSession(client, "owner@betafoods.com", "CorrectHorse42!")
        version_id = _product_version(a)
        response = b.post(
            f"/v1/product-versions/{version_id}/uploads",
            json={"filename": "label.jpg", "content_type": "image/jpeg"},
        )
        assert response.status_code == 404


class TestDownloadRouterTenancy:
    def test_download_url_for_a_foreign_key_is_404(self, owner: ApiSession) -> None:
        foreign_key = build_object_key(
            organization_id=uuid.uuid4(), product_version_id=uuid.uuid4(), filename="a.png"
        )
        response = owner.get("/v1/files/download-url", params={"key": foreign_key})
        assert response.status_code == 404

    def test_download_url_for_an_owned_key_succeeds(self, owner: ApiSession) -> None:
        own_key = build_object_key(
            organization_id=uuid.UUID(owner.org_id),
            product_version_id=uuid.uuid4(),
            filename="a.png",
        )
        response = owner.get("/v1/files/download-url", params={"key": own_key})
        assert response.status_code == 200
        assert response.json()["expires_in"] > 0


@pytest.mark.object_storage
@pytest.mark.skipif(not S3_ENDPOINT, reason="LABELLENS_TEST_S3_ENDPOINT is not set")
class TestLiveObjectStorage:
    """Proves the design goal directly: bytes reach the store without the API."""

    @pytest.fixture
    def live_client(self) -> S3ObjectStorage:
        return S3ObjectStorage(
            bucket="labellens-uploads-test",
            endpoint_url=S3_ENDPOINT,
            region="us-east-1",
            access_key=os.environ.get("LABELLENS_TEST_S3_ACCESS_KEY", "labellens"),
            secret_key=os.environ.get("LABELLENS_TEST_S3_SECRET_KEY", "labellens-dev-secret"),
            force_path_style=True,
        )

    def test_file_uploads_directly_to_storage_without_the_api(
        self, live_client: S3ObjectStorage
    ) -> None:
        key = f"test/{uuid.uuid4()}.bin"
        payload = b"this content never touches the labellens api process"

        put_url = live_client.generate_presigned_put(
            key, content_type="application/octet-stream", expires_in=60
        )
        put_response = httpx.put(
            put_url, content=payload, headers={"Content-Type": "application/octet-stream"}
        )
        assert put_response.status_code == 200

        get_url = live_client.generate_presigned_get(key, expires_in=60)
        get_response = httpx.get(get_url)
        assert get_response.status_code == 200
        assert get_response.content == payload

        meta = live_client.head_object(key)
        assert meta is not None
        assert meta.size_bytes == len(payload)

        live_client.delete_object(key)
        assert live_client.head_object(key) is None

    def test_expired_upload_url_is_rejected(self, live_client: S3ObjectStorage) -> None:
        key = f"test/{uuid.uuid4()}.bin"
        put_url = live_client.generate_presigned_put(
            key, content_type="application/octet-stream", expires_in=1
        )
        time.sleep(2)
        response = httpx.put(put_url, content=b"too late")
        assert response.status_code in (400, 403)
        assert live_client.head_object(key) is None

    def test_expired_download_url_is_rejected(self, live_client: S3ObjectStorage) -> None:
        key = f"test/{uuid.uuid4()}.bin"
        put_url = live_client.generate_presigned_put(
            key, content_type="application/octet-stream", expires_in=60
        )
        httpx.put(put_url, content=b"some bytes")

        get_url = live_client.generate_presigned_get(key, expires_in=1)
        time.sleep(2)
        response = httpx.get(get_url)
        assert response.status_code in (400, 403)

        live_client.delete_object(key)

    def test_head_object_is_none_for_a_missing_key(self, live_client: S3ObjectStorage) -> None:
        assert live_client.head_object(f"test/{uuid.uuid4()}.bin") is None
