"""Configuration items (CIs) and the identifiers they are known by.

CIs use joined-table inheritance: every CI has a row in `configuration_items` holding the
shared fields, plus a row in its type table (`devices`, `users`, `apps`) with the same id.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, Mapper, mapped_column, object_session, relationship

from cmdb.models.base import Base, str_enum, utcnow
from cmdb.models.enums import AppType, CIStatus, CIType, IdentifierKind

if TYPE_CHECKING:
    from cmdb.models.ingestion import SourceRecord
    from cmdb.models.relationships import CIRelationship


class ConfigurationItem(Base):
    __tablename__ = "configuration_items"
    __table_args__ = (Index("ix_configuration_items_type_name", "ci_type", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ci_type: Mapped[CIType] = mapped_column(str_enum(CIType))
    # Display name (hostname, full name, app name). Deliberately NOT unique: identity comes
    # only from ci_identifiers.
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[CIStatus] = mapped_column(str_enum(CIStatus), default=CIStatus.UNKNOWN)
    # Source fields that don't map to a typed column.
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Per field: {"observed_at": ..., "source_record_id": ...} of the value currently stored.
    field_provenance: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Maintained by _touch_updated_at below, not `onupdate`: onupdate only fires when this
    # table is updated, which misses changes to subclass-only columns (e.g. devices.location).
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)

    identifiers: Mapped[list["CIIdentifier"]] = relationship(
        back_populates="ci", cascade="all, delete-orphan", passive_deletes=True
    )
    outgoing: Mapped[list["CIRelationship"]] = relationship(
        foreign_keys="CIRelationship.source_ci_id",
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    incoming: Mapped[list["CIRelationship"]] = relationship(
        foreign_keys="CIRelationship.target_ci_id",
        back_populates="target",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # No delete cascade: source records outlive their CI (the FK is ON DELETE SET NULL).
    source_records: Mapped[list["SourceRecord"]] = relationship(
        back_populates="ci", passive_deletes=True
    )

    __mapper_args__ = {"polymorphic_on": "ci_type"}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id} name={self.name!r}>"


@event.listens_for(ConfigurationItem, "before_update", propagate=True)
def _touch_updated_at(_mapper: Mapper, _connection: Any, target: ConfigurationItem) -> None:
    """Bump updated_at when any column of the CI changes, in the base or the subclass table.

    Setting it here forces an UPDATE of `configuration_items` even when only the subclass
    table changed. Relationship collections are excluded; edges carry their own created_at.
    """
    session = object_session(target)
    if session is not None and session.is_modified(target, include_collections=False):
        target.updated_at = utcnow()


class Device(ConfigurationItem):
    __tablename__ = "devices"

    ci_id: Mapped[int] = mapped_column(
        ForeignKey("configuration_items.id", ondelete="CASCADE"), primary_key=True
    )
    hostname: Mapped[str | None] = mapped_column(String(255), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    mac_address: Mapped[str | None] = mapped_column(String(32))
    os_family: Mapped[str | None] = mapped_column(String(64))
    os_version: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[str | None] = mapped_column(String(255))
    encryption_enabled: Mapped[bool | None]
    device_type: Mapped[str | None] = mapped_column(String(64))
    serial_number: Mapped[str | None] = mapped_column(String(128), index=True)
    last_seen: Mapped[datetime | None]

    __mapper_args__ = {"polymorphic_identity": CIType.DEVICE}


class User(ConfigurationItem):
    __tablename__ = "users"

    ci_id: Mapped[int] = mapped_column(
        ForeignKey("configuration_items.id", ondelete="CASCADE"), primary_key=True
    )
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    department: Mapped[str | None] = mapped_column(String(255))
    team: Mapped[str | None] = mapped_column(String(255))
    groups: Mapped[list[str]] = mapped_column(default=list)
    mfa_enabled: Mapped[bool | None]
    last_login: Mapped[datetime | None]

    __mapper_args__ = {"polymorphic_identity": CIType.USER}


class App(ConfigurationItem):
    __tablename__ = "apps"

    ci_id: Mapped[int] = mapped_column(
        ForeignKey("configuration_items.id", ondelete="CASCADE"), primary_key=True
    )
    vendor: Mapped[str | None] = mapped_column(String(255))
    app_type: Mapped[AppType | None] = mapped_column(str_enum(AppType))
    category: Mapped[str | None] = mapped_column(String(255))
    owner_team: Mapped[str | None] = mapped_column(String(255))
    reported_usage_count: Mapped[int | None]
    sso_enabled: Mapped[bool | None]
    # True when the app has only been seen as a reference (an Okta assignment or an
    # integration target), never as a full inventory record.
    is_stub: Mapped[bool] = mapped_column(default=False)

    __mapper_args__ = {"polymorphic_identity": CIType.APP}


class CIIdentifier(Base):
    """A strong key a CI is known by (email, serial, source-system id, ...).

    `source_system` scopes identifiers that are only meaningful inside the system that issued
    them: Okta's user id "u_110" and another tool's "u_110" are unrelated. It is required for
    `external_id` and must be empty for org-wide keys (email, serial, ...), which have to match
    across sources. Empty string rather than NULL, because NULLs never collide in a UNIQUE
    constraint and would let one email attach to two CIs.
    """

    __tablename__ = "ci_identifiers"
    __table_args__ = (
        UniqueConstraint("kind", "source_system", "value", name="uq_identifier"),
        CheckConstraint(
            "(kind = 'external_id' AND source_system <> '')"
            " OR (kind <> 'external_id' AND source_system = '')",
            name="ck_identifier_source_system_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ci_id: Mapped[int] = mapped_column(
        ForeignKey("configuration_items.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[IdentifierKind] = mapped_column(str_enum(IdentifierKind))
    source_system: Mapped[str] = mapped_column(String(64), default="")
    value: Mapped[str] = mapped_column(String(320))

    ci: Mapped[ConfigurationItem] = relationship(back_populates="identifiers")
