"""ORM models for configuration items (CIs), their relationships, and ingestion provenance.

Every model module is imported here so all tables are registered on `Base.metadata` (and
string relationship targets resolve) as soon as anything imports `cmdb.models`.
"""

from cmdb.models.base import Base, UTCDateTime, str_enum, utcnow
from cmdb.models.ci import App, CIIdentifier, ConfigurationItem, Device, User
from cmdb.models.enums import (
    AppType,
    CIStatus,
    CIType,
    IdentifierKind,
    IngestionStatus,
    MappingSource,
    RelType,
)
from cmdb.models.ingestion import IngestionRun, SchemaMapping, SourceRecord
from cmdb.models.relationships import CIRelationship

__all__ = [
    "App",
    "AppType",
    "Base",
    "CIIdentifier",
    "CIRelationship",
    "CIStatus",
    "CIType",
    "ConfigurationItem",
    "Device",
    "IdentifierKind",
    "IngestionRun",
    "IngestionStatus",
    "MappingSource",
    "RelType",
    "SchemaMapping",
    "SourceRecord",
    "UTCDateTime",
    "User",
    "str_enum",
    "utcnow",
]
