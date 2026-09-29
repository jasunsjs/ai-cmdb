"""Errors raised while parsing, normalizing and ingesting input data."""

from typing import Any

from cmdb.errors import AIStepError

__all__ = [
    "AIStepError",
    "IngestError",
    "MappingGenerationError",
    "ParseError",
    "UnrecognizedValueError",
]


class ParseError(ValueError):
    """The uploaded file can't be turned into records (bad encoding, syntax, or shape)."""


class UnrecognizedValueError(ValueError):
    """A value is present but the deterministic rules can't classify it.

    Raised instead of guessing, so the caller can collect these and resolve them another way
    (e.g. one batched LLM call) or leave the field empty.
    """

    def __init__(self, value: Any, expected: str) -> None:
        self.value = value
        self.expected = expected
        super().__init__(f"Unrecognized {expected}: {value!r}")


class MappingGenerationError(AIStepError):
    """No valid mapping could be produced for a source format."""


class IngestError(Exception):
    """An ingestion failed; carries the HTTP status to report and the run it belongs to."""

    def __init__(self, status_code: int, message: str, ingestion_id: int | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.ingestion_id = ingestion_id
        super().__init__(message)
