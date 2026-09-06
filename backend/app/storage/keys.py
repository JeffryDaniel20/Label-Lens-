"""Object storage key scheme.

Every key is namespaced by organization so that ownership can be checked from
the key alone, without a database round-trip: `org/{org_id}/pv/{version_id}/{uuid}{ext}`.
Uploaded content is untrusted from the first byte, so the original filename is
never used verbatim in the key - only its extension survives, lowercased and
restricted to a small allowlist.
"""

from __future__ import annotations

import re
import uuid

_SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,8}$")

# Extension allowlist for the key scheme only. The actual MIME/magic-byte
# validation of file content happens at upload completion (P2-T3).
ALLOWED_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "heic", "tif", "tiff", "pdf"})


def extract_extension(filename: str) -> str:
    _, _, ext = filename.rpartition(".")
    ext = ext.lower()
    if not ext or not _SAFE_EXTENSION.match(ext) or ext not in ALLOWED_EXTENSIONS:
        return "bin"
    return ext


def build_object_key(
    *, organization_id: uuid.UUID, product_version_id: uuid.UUID, filename: str
) -> str:
    ext = extract_extension(filename)
    return f"org/{organization_id}/pv/{product_version_id}/{uuid.uuid4()}.{ext}"


def build_render_key(
    *, organization_id: uuid.UUID, product_version_id: uuid.UUID, sha256: str, page_no: int
) -> str:
    """Content-addressed by sha256, not by file id, so dedup'd files share renders."""
    return f"org/{organization_id}/pv/{product_version_id}/render/{sha256}/{page_no:04d}.png"


def build_report_pdf_key(
    *, organization_id: uuid.UUID, product_version_id: uuid.UUID, report_id: uuid.UUID
) -> str:
    """Nested under the same `pv` segment as every other key (P7-T2), not a
    parallel `org/{org}/reports/...` scheme - `key_belongs_to_org`'s ownership
    check only recognizes the one prefix, and a second scheme would need its
    own check duplicated alongside it for no real benefit."""
    return f"org/{organization_id}/pv/{product_version_id}/reports/{report_id}.pdf"


def key_belongs_to_org(key: str, organization_id: uuid.UUID) -> bool:
    """True if `key` was minted under this organization's namespace.

    This lets ownership be verified from the key string alone - no lookup
    table is required to reject a cross-tenant download or upload request.
    """
    prefix = f"org/{organization_id}/pv/"
    return key.startswith(prefix) and "/../" not in key and not key.startswith("/")


def key_product_version_id(key: str) -> uuid.UUID | None:
    parts = key.split("/")
    if len(parts) >= 4 and parts[0] == "org" and parts[2] == "pv":
        try:
            return uuid.UUID(parts[3])
        except ValueError:
            return None
    return None
