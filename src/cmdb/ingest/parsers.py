"""Bytes -> parsed document: format detection and JSON / YAML / CSV parsing."""

import csv
import io
import json
from enum import StrEnum
from pathlib import PurePath
from typing import Any

import yaml

from cmdb.ingest.errors import ParseError


class Format(StrEnum):
    JSON = "json"
    YAML = "yaml"
    CSV = "csv"


_EXTENSIONS = {".json": Format.JSON, ".yaml": Format.YAML, ".yml": Format.YAML, ".csv": Format.CSV}


def decode(content: bytes) -> str:
    """Decode UTF-8 (dropping a byte-order mark) and reject empty files."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("File is not valid UTF-8 text") from exc
    if not text.strip():
        raise ParseError("File is empty")
    return text


def detect_format(filename: str | None, text: str) -> Format:
    """Detect the format from the file extension, falling back to sniffing the content."""
    suffix = PurePath(filename or "").suffix.lower()
    if suffix in _EXTENSIONS:
        return _EXTENSIONS[suffix]

    # JSON first: every JSON document is also valid YAML.
    for fmt in (Format.JSON, Format.YAML):
        try:
            parse_document(text, fmt)
        except ParseError:
            continue
        return fmt
    try:
        rows = _parse_csv(text)
    except ParseError:
        rows = []
    # Almost any text "parses" as a one-column CSV, so require real columns.
    if rows and len(rows[0]) >= 2:
        return Format.CSV
    raise ParseError("Could not detect the file format; expected JSON, YAML or CSV")


def parse_document(text: str, fmt: Format) -> Any:
    match fmt:
        case Format.JSON:
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ParseError(f"Invalid JSON: {exc}") from exc
        case Format.YAML:
            return _parse_yaml(text)
        case Format.CSV:
            return _parse_csv(text)


def _parse_yaml(text: str) -> Any:
    # BaseLoader keeps every scalar as a string. The default loaders silently change data:
    # `version: 14.10` becomes 14.1, `country: NO` becomes False, unquoted dates become
    # date objects. Typing values is the normalizers' job. BaseLoader only builds plain
    # dicts, lists and strings, so it is as safe as safe_load.
    try:
        document = yaml.load(text, Loader=yaml.BaseLoader)
    except yaml.YAMLError as exc:
        raise ParseError(f"Invalid YAML: {exc}") from exc
    if not isinstance(document, dict | list):
        raise ParseError("YAML document must be a mapping or a list")
    return document


def _parse_csv(text: str) -> list[dict[str, str | None]]:
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            text[:4096], delimiters=",;\t|"
        )
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ParseError("CSV has no header row")
    headers = [name.strip() for name in reader.fieldnames]
    if any(not name for name in headers):
        raise ParseError("CSV header has an empty column name")
    reader.fieldnames = headers

    rows = []
    for line_number, row in enumerate(reader, start=2):
        if None in row:
            raise ParseError(f"CSV line {line_number} has more values than the header")
        cleaned = {key: _clean_cell(value) for key, value in row.items()}
        if any(value is not None for value in cleaned.values()):
            rows.append(cleaned)
    return rows


def _clean_cell(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None
