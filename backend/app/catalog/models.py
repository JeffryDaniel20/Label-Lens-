"""Product catalog: products and their immutable label versions."""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey, enum_column


class ProductVersionStatus(enum.StrEnum):
    DRAFT = "draft"
    LOCKED = "locked"
    SUPERSEDED = "superseded"


class FileStatus(enum.StrEnum):
    UPLOADING = "uploading"
    VALIDATING = "validating"
    READY = "ready"
    REJECTED = "rejected"


class AvStatus(enum.StrEnum):
    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"
    SKIPPED = "skipped"
    ERROR = "error"


class Product(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("organization_id", "internal_sku", name="uq_products_org_sku"),
        Index("ix_products_org_created", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    internal_sku: Mapped[str] = mapped_column(String(80), nullable=False)
    category_hint: Mapped[str | None] = mapped_column(String(80), default=None)
    market_codes: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    versions: Mapped[list[ProductVersion]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductVersion(UUIDPrimaryKey, TimestampMixin, Base):
    """A specific artwork revision.

    A version becomes immutable (`locked_at`) as soon as anything downstream -
    an analysis, a report - depends on it, so historical results stay valid.
    """

    __tablename__ = "product_versions"
    __table_args__ = (
        UniqueConstraint("product_id", "version_no", name="uq_product_versions_product_no"),
        Index("ix_product_versions_org_product", "organization_id", "product_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[ProductVersionStatus] = mapped_column(
        enum_column(ProductVersionStatus), nullable=False, default=ProductVersionStatus.DRAFT
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    product: Mapped[Product] = relationship(back_populates="versions")

    @property
    def is_locked(self) -> bool:
        return self.locked_at is not None


class File(UUIDPrimaryKey, TimestampMixin, Base):
    """A validated upload attached to a product version.

    `storage_key` is not necessarily the key the caller originally uploaded
    to: when the same content (by `sha256`) already exists anywhere in the
    organization, the duplicate object is deleted and this row points at the
    pre-existing key instead - the dedup the schema calls for.
    """

    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_org_product_version", "organization_id", "product_version_id"),
        Index("ix_files_org_sha256", "organization_id", "sha256"),
        Index("ix_files_org_storage_key", "organization_id", "storage_key"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    product_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("product_versions.id", ondelete="CASCADE"), nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(400), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[FileStatus] = mapped_column(
        enum_column(FileStatus), nullable=False, default=FileStatus.VALIDATING
    )
    av_status: Mapped[AvStatus] = mapped_column(
        enum_column(AvStatus), nullable=False, default=AvStatus.PENDING
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # P7-T8 retention purge: set once the underlying object-storage bytes are
    # deleted. The row itself (and every downstream finding/evidence/report)
    # is deliberately kept - only the source image is gone (IMPLEMENTATION.md
    # 16's "the snippet remains; the image link 404s gracefully").
    purged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class FilePage(UUIDPrimaryKey, TimestampMixin, Base):
    """A normalized, EXIF-free raster of one page of a `File`.

    `render_key` is content-addressed by the file's sha256 rather than by
    `file_id` - `org/{org}/pv/{pv}/render/{sha256}/{page_no}.png` - so two
    `File` rows that dedup to the same content also share the same rendered
    pages instead of re-rasterizing and re-storing identical output.
    """

    __tablename__ = "file_pages"
    __table_args__ = (
        UniqueConstraint("file_id", "page_no", name="uq_file_pages_file_page_no"),
        Index("ix_file_pages_org_file", "organization_id", "file_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    render_key: Mapped[str] = mapped_column(String(400), nullable=False)
    purged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
