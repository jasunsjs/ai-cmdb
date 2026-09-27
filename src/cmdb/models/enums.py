"""Enumerations stored as plain strings in the database (see `str_enum`)."""

from enum import StrEnum


class CIType(StrEnum):
    DEVICE = "device"
    USER = "user"
    APP = "app"


class CIStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


class AppType(StrEnum):
    SAAS = "saas"
    ON_PREM = "on_prem"


class RelType(StrEnum):
    OWNS = "OWNS"  # user -> device
    USES = "USES"  # user -> app
    INTEGRATES_WITH = "INTEGRATES_WITH"  # app -> app
    DEPENDS_ON = "DEPENDS_ON"  # reserved for app/device dependencies


class IdentifierKind(StrEnum):
    EMAIL = "email"
    EMPLOYEE_ID = "employee_id"
    EXTERNAL_ID = "external_id"
    SERIAL = "serial"
    MAC = "mac"
    HOSTNAME = "hostname"
    ASSET_TAG = "asset_tag"
    ALIAS = "alias"


class IngestionStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class MappingSource(StrEnum):
    LLM = "llm"
    MANUAL = "manual"
