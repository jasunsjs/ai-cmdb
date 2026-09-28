"""Errors raised while parsing and normalizing input data."""

from typing import Any


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
