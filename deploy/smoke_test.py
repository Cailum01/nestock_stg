"""B2 Demo-day smoke test: hit the deployed API's existing endpoints, in order.

Checklist §6 "Health / smoke test passes before presentation".

Rules:
- Stops at the FIRST failing step and exits 1. No retry, no skip, and no mock stands in
  for a real service: a mock Speech answer counts as a failure.
- Read-only for domain data: it asks questions and uploads one audio clip for
  transcription. `/agent/messages` writes no domain data (AGENTS.md HITL rule 8).
- Standard library only, so it runs on the presenter's laptop with no venv. It holds
  no credential and prints none; the base URL is the only input.
- Targets the cloud HTTPS endpoint. localhost / 10.0.2.2 are refused unless
  `--allow-local` is given (rehearsal on a dev machine only, never for Final Demo).

Steps (see deploy/demo-day-runbook.md):
  1 /health  2 /inventory  3 /shopping  4 /forecast  5 speech  6 agent

Usage:
  python3 deploy/smoke_test.py --base-url https://<demo-host>
  NESTOCK_BASE_URL=https://<demo-host> python3 deploy/smoke_test.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "data" / "demo_household_manifest.json"
DEFAULT_AUDIO = Path(__file__).resolve().parent / "smoke-audio-detergent.wav"

HOUSEHOLD_ID = "demo-household"

# What MockSpeechToTextService returns for any clip over 64 bytes. Seeing it means the
# deployed runtime is running with NESTOCK_SPEECH_MOCK or without real Speech.
MOCK_SPEECH_TRANSCRIPT = "幫我查一下衛生紙"
# The test clip says "洗衣精還有幾瓶". A real transcription contains this word.
SPEECH_EXPECTED_WORD = "洗衣精"

# Agent question and the facts the Demo baseline must give back (checklist §5).
AGENT_QUESTION = "衛生紙還有幾包？放在哪裡？"
AGENT_EXPECTED_WORDS = ("8 包", "浴室櫃子")

# Expected forecast story, B2 frozen table (days_until_empty is the purchase-interval
# rate, so it does not drift with the day the question is asked).
EXPECTED_FORECAST = {
    "seed-detergent": {"suggested_action": "consider_restock", "days_until_empty": 3.0},
    "seed-tissue": {"suggested_action": "none", "days_until_empty": 40.0},
    "seed-flour": {"suggested_action": "consider_restock", "days_until_empty": 0.0},
}
DAYS_TOLERANCE = 0.5

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "10.0.2.2", "::1"}


class SmokeFailure(Exception):
    """A step failed; the run stops here."""


class Client:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, bytes]:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SmokeFailure(f"{method} {path}: cannot reach the server ({error})") from error

    def get_json(self, path: str) -> object:
        status, payload = self.request("GET", path)
        return parse_json(f"GET {path}", status, payload)

    def post_json(self, path: str, document: dict) -> object:
        status, payload = self.request(
            "POST",
            path,
            body=json.dumps(document, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
        return parse_json(f"POST {path}", status, payload)


def parse_json(label: str, status: int, payload: bytes) -> object:
    if status != 200:
        raise SmokeFailure(f"{label}: expected HTTP 200, got {status} {_excerpt(payload)}")
    try:
        return json.loads(payload)
    except ValueError as error:
        raise SmokeFailure(f"{label}: response is not JSON ({_excerpt(payload)})") from error


def _excerpt(payload: bytes, limit: int = 200) -> str:
    return payload.decode("utf-8", errors="replace")[:limit]


def load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SmokeFailure(f"baseline manifest unreadable: {path} ({error})") from error


def require_endpoint_is_cloud(base_url: str, allow_local: bool) -> None:
    parts = urllib.parse.urlsplit(base_url)
    if parts.hostname is None:
        raise SmokeFailure(f"not a URL: {base_url!r}")
    is_local = parts.hostname in LOCAL_HOSTS or parts.hostname.endswith(".local")
    if is_local and not allow_local:
        raise SmokeFailure(
            f"{parts.hostname} is a local address; Final Demo must use the cloud HTTPS "
            "endpoint (checklist §1). Use --allow-local only for a dev rehearsal."
        )
    if parts.scheme != "https" and not (allow_local and is_local):
        raise SmokeFailure(f"base URL must be https://, got {parts.scheme}://")


# -- steps --------------------------------------------------------------------------------


def step_health(client: Client, ctx: dict) -> str:
    body = client.get_json("/health")
    if body != {"status": "ok"}:
        raise SmokeFailure(f"/health returned {body!r}, expected {{'status': 'ok'}}")
    return "status ok"


def step_inventory(client: Client, ctx: dict) -> str:
    rows = client.get_json("/inventory")
    if not isinstance(rows, list):
        raise SmokeFailure("/inventory did not return a list")
    expected_rows = ctx["manifest"]["inventory"]
    problems = []
    seen = [row.get("item_id") for row in rows]
    duplicates = sorted({str(i) for i in seen if seen.count(i) > 1})
    if duplicates:
        problems.append("duplicate item_id: " + ", ".join(duplicates))
    expected_ids = {row["item_id"] for row in expected_rows}
    extra = sorted(str(i) for i in set(seen) - expected_ids)
    if extra:
        problems.append("unexpected items: " + ", ".join(extra))
    by_id = {row.get("item_id"): row for row in rows}
    for expected in expected_rows:
        row = by_id.get(expected["item_id"])
        if row is None:
            problems.append(f"missing {expected['item_id']}")
            continue
        for field in ("name", "quantity", "unit", "location"):
            if row.get(field) != expected[field]:
                problems.append(
                    f"{expected['item_id']}.{field}: {row.get(field)!r} != {expected[field]!r}"
                )
    for row in rows:
        if row.get("household_id") != HOUSEHOLD_ID:
            problems.append(f"{row.get('item_id')} is not in {HOUSEHOLD_ID}")
    if len(rows) != len(expected_rows):
        problems.append(f"{len(rows)} rows, baseline has {len(expected_rows)}")
    if problems:
        raise SmokeFailure("inventory differs from baseline: " + "; ".join(problems))
    return f"exactly {len(expected_rows)} baseline items match, no extra"


def step_shopping(client: Client, ctx: dict) -> str:
    rows = client.get_json("/shopping")
    if not isinstance(rows, list):
        raise SmokeFailure("/shopping did not return a list")
    expected_rows = ctx["manifest"]["shopping"]
    expected_ids = {row["shopping_item_id"] for row in expected_rows}
    problems = []
    # Key by shopping_item_id: a baseline row (e.g. 牙膏) may not link to an inventory item.
    # Rows of every status are checked: a stray completed row is also a non-baseline state.
    seen = [row.get("shopping_item_id") for row in rows]
    duplicates = sorted({str(i) for i in seen if seen.count(i) > 1})
    if duplicates:
        problems.append("duplicate shopping_item_id: " + ", ".join(duplicates))
    extra = sorted(str(i) for i in set(seen) - expected_ids)
    if extra:
        problems.append("unexpected rows (any status): " + ", ".join(extra))
    by_id = {row.get("shopping_item_id"): row for row in rows}
    for expected in expected_rows:
        key = expected["shopping_item_id"]
        row = by_id.get(key)
        if row is None:
            problems.append(f"missing row {key}")
        elif row.get("status") != "pending":
            problems.append(f"{key} status {row.get('status')!r}, expected 'pending'")
        elif (row.get("name"), row.get("quantity"), row.get("unit")) != (
            expected["name"],
            expected["quantity"],
            expected["unit"],
        ):
            problems.append(
                f"{key}: {row.get('name')} {row.get('quantity')}{row.get('unit')} "
                f"!= {expected['name']} {expected['quantity']}{expected['unit']}"
            )
    for row in rows:
        if row.get("household_id") != HOUSEHOLD_ID:
            problems.append(f"{row.get('shopping_item_id')} is not in {HOUSEHOLD_ID}")
    if len(rows) != len(expected_rows):
        problems.append(f"{len(rows)} rows, baseline has {len(expected_rows)}")
    if problems:
        raise SmokeFailure("shopping differs from baseline: " + "; ".join(problems))
    return f"exactly {len(expected_ids)} baseline pending rows match, no extra"


def step_forecast(client: Client, ctx: dict) -> str:
    rows = client.get_json("/forecast")
    if not isinstance(rows, list):
        raise SmokeFailure("/forecast did not return a list")
    by_id = {row.get("item_id"): row for row in rows}
    problems = []
    for item_id, expected in EXPECTED_FORECAST.items():
        row = by_id.get(item_id)
        if row is None:
            problems.append(f"{item_id} missing")
            continue
        if row.get("status") == "insufficient_data":
            problems.append(f"{item_id} is insufficient_data (expected an estimate)")
            continue
        if row.get("suggested_action") != expected["suggested_action"]:
            problems.append(
                f"{item_id}.suggested_action {row.get('suggested_action')!r} "
                f"!= {expected['suggested_action']!r}"
            )
        days = row.get("days_until_empty")
        if days is None or abs(days - expected["days_until_empty"]) > DAYS_TOLERANCE:
            problems.append(
                f"{item_id}.days_until_empty {days!r} != {expected['days_until_empty']}"
            )
    inventory = {r["item_id"]: r for r in ctx["manifest"]["inventory"]}
    for item_id, row in by_id.items():
        if item_id in inventory and row.get("confirmed_quantity") != inventory[item_id]["quantity"]:
            problems.append(f"{item_id}.confirmed_quantity changed by forecast")
    if problems:
        raise SmokeFailure("forecast differs from baseline: " + "; ".join(problems))
    return "洗衣精 / 麵粉 restock, 衛生紙 normal"


def multipart(
    fields: dict[str, str], file_field: str, filename: str, data: bytes, content_type: str
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        header = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
        parts.append(f"{header}{value}\r\n".encode())
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode("utf-8")
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def step_speech(client: Client, ctx: dict) -> str:
    audio_path: Path = ctx["audio"]
    try:
        audio = audio_path.read_bytes()
    except OSError as error:
        raise SmokeFailure(f"test audio unreadable: {audio_path} ({error})") from error
    body, content_type = multipart(
        {"language": "zh-TW"}, "file", audio_path.name, audio, "audio/wav"
    )
    status, payload = client.request(
        "POST", "/api/speech/transcribe", body=body, content_type=content_type
    )
    result = parse_json("POST /api/speech/transcribe", status, payload)
    transcript = str(result.get("transcript", "")) if isinstance(result, dict) else ""
    if transcript.strip() == MOCK_SPEECH_TRANSCRIPT:
        raise SmokeFailure(
            "speech returned the fixed MOCK transcript: NESTOCK_SPEECH_MOCK is set or "
            "real Speech is not configured on the deployed runtime"
        )
    if SPEECH_EXPECTED_WORD not in transcript:
        raise SmokeFailure(
            f"speech transcript {transcript!r} does not contain {SPEECH_EXPECTED_WORD!r}"
        )
    return f"real transcript: {transcript.strip()!r}"


def step_agent(client: Client, ctx: dict) -> str:
    result = client.post_json(
        "/agent/messages", {"household_id": HOUSEHOLD_ID, "message": AGENT_QUESTION}
    )
    if not isinstance(result, dict):
        raise SmokeFailure("/agent/messages did not return an object")
    message = str(result.get("message", ""))
    missing = [word for word in AGENT_EXPECTED_WORDS if word not in message]
    if missing:
        raise SmokeFailure(f"agent answer lacks {missing}; answer was: {message[:200]!r}")
    if result.get("pending_approval") is not None:
        raise SmokeFailure("a read-only question produced an approval card")
    return "answer mentions 8 包 / 浴室櫃子"


STEPS: list[tuple[str, Callable[[Client, dict], str]]] = [
    ("1 /health", step_health),
    ("2 /inventory", step_inventory),
    ("3 /shopping", step_shopping),
    ("4 /forecast", step_forecast),
    ("5 speech transcribe", step_speech),
    ("6 agent question", step_agent),
]


def run(client: Client, ctx: dict, out=print) -> int:
    for label, step in STEPS:
        started = time.perf_counter()
        try:
            detail = step(client, ctx)
        except SmokeFailure as failure:
            out(f"FAIL  {label}: {failure}")
            out("\nSTOP: smoke test failed. Do not start the Demo; no mock replaces this step.")
            return 1
        out(f"PASS  {label} ({time.perf_counter() - started:.1f}s) {detail}")
    out(f"\nALL {len(STEPS)} STEPS PASSED against {client.base_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default=os.environ.get("NESTOCK_BASE_URL", ""))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per request")
    parser.add_argument("--allow-local", action="store_true", help="dev rehearsal only")
    args = parser.parse_args(argv)

    if not args.base_url:
        print("error: give --base-url or set NESTOCK_BASE_URL", file=sys.stderr)
        return 2
    try:
        require_endpoint_is_cloud(args.base_url, args.allow_local)
        ctx = {"manifest": load_manifest(args.manifest), "audio": args.audio}
    except SmokeFailure as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 2
    return run(Client(args.base_url, args.timeout), ctx)


if __name__ == "__main__":
    raise SystemExit(main())
