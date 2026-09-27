"""Declarative base and column helpers shared by all ORM models."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Dialect, Enum, TypeDecorator
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores timezone-aware datetimes as UTC and returns them timezone-aware.

    SQLite has no timezone support, so values are normalized to UTC on the way in and
    UTC is re-attached on the way out. Naive datetimes are rejected to surface bugs early.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"Naive datetime not allowed, attach a timezone: {value!r}")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def str_enum(enum_cls: type[StrEnum]) -> Enum:
    """A VARCHAR column with a CHECK constraint that stores the enum's value, not its name."""
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
        name=f"{enum_cls.__name__.lower()}_enum",
    )


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: UTCDateTime,
        dict[str, Any]: MutableDict.as_mutable(JSON),
        list[str]: MutableList.as_mutable(JSON),
    }
