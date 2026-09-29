"""Errors shared across the ingest and ask services."""


class AIStepError(Exception):
    """An AI-assisted step (mapping, classification, matching, SQL) failed or is unavailable."""
