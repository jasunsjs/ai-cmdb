"""Typed, directed edges between configuration items."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cmdb.models.base import Base, str_enum, utcnow
from cmdb.models.ci import ConfigurationItem
from cmdb.models.enums import RelType


class CIRelationship(Base):
    __tablename__ = "relationships"
    __table_args__ = (
        UniqueConstraint("source_ci_id", "target_ci_id", "rel_type", name="uq_relationship"),
        CheckConstraint("source_ci_id <> target_ci_id", name="ck_relationship_no_self_loop"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_relationship_confidence"),
        # A device has at most one owner.
        Index(
            "uq_single_owner",
            "target_ci_id",
            unique=True,
            sqlite_where=text("rel_type = 'OWNS'"),
            postgresql_where=text("rel_type = 'OWNS'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_ci_id: Mapped[int] = mapped_column(
        ForeignKey("configuration_items.id", ondelete="CASCADE")
    )
    target_ci_id: Mapped[int] = mapped_column(
        ForeignKey("configuration_items.id", ondelete="CASCADE"), index=True
    )
    rel_type: Mapped[RelType] = mapped_column(str_enum(RelType))
    confidence: Mapped[float] = mapped_column(default=1.0)
    ingestion_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    source: Mapped[ConfigurationItem] = relationship(
        foreign_keys=[source_ci_id], back_populates="outgoing"
    )
    target: Mapped[ConfigurationItem] = relationship(
        foreign_keys=[target_ci_id], back_populates="incoming"
    )
