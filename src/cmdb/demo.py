"""`uv run cmdb-demo`: exercise a running server with the sample files and questions.

Start the server first (`uv run cmdb-serve`). Uses only the standard library. Every response
is printed briefly and saved as JSON under --out, so it doubles as sample API output.
Re-running is safe: ingestion is idempotent.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

SAMPLE_FILES = [
    "sample_okta.json",
    "sample_app.json",
    "sample_hardware.csv",
    "sample_hardware.json",
    "sample_hardware.yaml",
]

QUESTIONS = [
    "Which users don't have MFA?",
    "Who owns laptop-jdoe and where is it?",
    "Which apps integrate with Slack?",
    "Which devices are inactive?",
    "Which apps don't have SSO?",
    "Which users use GitHub?",
    "Compare each app's reported number of users with the users we actually observed.",
    "Which apps have the highest annual cost?",
    "Which apps are hosted in AWS, and are they SaaS or on-prem?",
]


def encode_multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body with one file field; returns (body, content type)."""
    boundary = uuid.uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def request(
    base_url: str, method: str, path: str, body: bytes | None = None, content_type: str = ""
) -> tuple[int, Any]:
    req = urllib.request.Request(f"{base_url}{path}", data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--input-dir", type=Path, default=Path("input_data"))
    parser.add_argument("--out", type=Path, default=Path("examples"))
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")

    try:
        _, health = request(base_url, "GET", "/health")
    except urllib.error.URLError as exc:
        print(f"Can't reach {base_url} ({exc.reason}). Start it with: uv run cmdb-serve")
        return 1
    if not health.get("llm_configured"):
        print("Warning: the server has no LLM configured; new formats and /ask will fail.")
    args.out.mkdir(parents=True, exist_ok=True)

    print("== Ingest")
    for filename in SAMPLE_FILES:
        body, content_type = encode_multipart(
            "file", filename, (args.input_dir / filename).read_bytes()
        )
        status, report = request(base_url, "POST", "/ingest", body, content_type)
        _save(args.out / f"ingest_{Path(filename).name.replace('.', '_')}.json", report)
        if status != 200:
            print(f"  {filename}: HTTP {status}: {report.get('detail')}")
            continue
        counts = ", ".join(
            f"{ci_type} +{c['created']}/~{c['updated']}" for ci_type, c in report["cis"].items()
        )
        ai = sum(1 for d in report["ai_decisions"] if d["type"] != "schema_mapping")
        print(
            f"  {filename}: {report['records']} records, mapping from {report['mapping_source']}, "
            f"{counts}, AI decisions: {ai}, warnings: {len(report['warnings'])}"
        )

    print("\n== Ask")
    for number, question in enumerate(QUESTIONS, start=1):
        body = json.dumps({"question": question}).encode()
        status, answer = request(base_url, "POST", "/ask", body, "application/json")
        _save(args.out / f"ask_{number}.json", answer)
        print(f"\nQ: {question}")
        if status != 200:
            print(f"   HTTP {status}: {answer.get('detail')}")
            continue
        print(f"A: {answer['answer']}")
        print(f"   SQL: {answer['sql']}")

    print(f"\nResponses saved to {args.out}/")
    return 0


def _save(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
