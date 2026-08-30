"""S3-compatible object storage adapter.

Talks to any S3-compatible endpoint (MinIO locally, Cloudflare R2 or Backblaze
B2 in production) through the same boto3 client, configured entirely by
`Settings`. The application itself never proxies file bytes: it only mints
short-lived presigned URLs so uploads and downloads go directly between the
caller and the object store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import boto3
from botocore.client import Config as BotoConfig

from app.platform.config import Settings


class ObjectStorageClient(Protocol):
    def generate_presigned_put(self, key: str, *, content_type: str, expires_in: int) -> str: ...

    def generate_presigned_get(self, key: str, *, expires_in: int) -> str: ...

    def head_object(self, key: str) -> ObjectMeta | None: ...

    def download_object(self, key: str) -> bytes: ...

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None: ...

    def delete_object(self, key: str) -> None: ...


@dataclass(slots=True, frozen=True)
class ObjectMeta:
    size_bytes: int
    content_type: str | None
    etag: str | None


class S3ObjectStorage:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        region: str,
        access_key: str,
        secret_key: str,
        force_path_style: bool = True,
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path" if force_path_style else "auto"},
            ),
        )

    def generate_presigned_put(self, key: str, *, content_type: str, expires_in: int) -> str:
        url: str = self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
        )
        return url

    def generate_presigned_get(self, key: str, *, expires_in: int) -> str:
        url: str = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        return url

    def head_object(self, key: str) -> ObjectMeta | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise
        return ObjectMeta(
            size_bytes=response["ContentLength"],
            content_type=response.get("ContentType"),
            etag=response.get("ETag"),
        )

    def download_object(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)


def build_storage_client(settings: Settings) -> S3ObjectStorage:
    return S3ObjectStorage(
        bucket=settings.storage_bucket,
        endpoint_url=settings.storage_endpoint_url or None,
        region=settings.storage_region,
        access_key=settings.storage_access_key,
        secret_key=settings.storage_secret_key,
        force_path_style=settings.storage_force_path_style,
    )
