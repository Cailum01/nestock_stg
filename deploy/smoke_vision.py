"""A2 Demo-day Vision smoke test: hit the deployed API's media and Agent endpoints in order.

Issue #156 "雲端 Vision 連線尚無獨立證據（本機已通過），需 A2 說明如何在雲端驗證".

Rules:
- Standard library only, so it runs on any machine with no venv or external packages.
- Holds no credential and prints none; the base URL is the only required input.
- Read-only for domain data: uploads a temporary test photo and queries the Agent.
  `/agent/messages` writes no domain data (AGENTS.md HITL rule 8).
- Stops at the FIRST failing step and exits 1. No mock stands in for real Vision:
  a 501 / 502 or missing vision result counts as a failure.

Steps:
  1 upload authorization (POST /api/media/upload-authorization)
  2 binary photo upload  (PUT <upload_url>)
  3 agent vision query   (POST /agent/messages with image_ref)

Usage:
  python deploy/smoke_vision.py --base-url https://<demo-host>
  NESTOCK_BASE_URL=https://<demo-host> python deploy/smoke_vision.py
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE_IMAGE = REPO_ROOT / "backend" / "tests" / "fixtures" / "vision" / "img_single.jpg"
HOUSEHOLD_ID = "demo-household"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "10.0.2.2", "::1"}


class SmokeFailure(Exception):
    """A step failed; the smoke test stops immediately."""


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
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        req_headers = {"Accept": "application/json"}
        if headers:
            req_headers.update(headers)
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SmokeFailure(f"{method} {path}: cannot reach the server ({error})") from error

    def post_json(self, path: str, document: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        status, raw = self.request("POST", path, body=payload, headers=headers)
        return parse_json(f"POST {path}", status, raw)

    def put_binary(
        self, url: str, data: bytes, headers: dict[str, str] | None = None
    ) -> tuple[int, bytes]:
        return self.request("PUT", url, body=data, headers=headers)


def parse_json(label: str, status: int, payload: bytes) -> dict[str, Any]:
    excerpt = payload.decode("utf-8", errors="replace")[:200]
    if status != 200:
        raise SmokeFailure(f"{label}: expected HTTP 200, got {status} {excerpt}")
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise SmokeFailure(f"{label}: expected JSON object, got {type(data).__name__}")
        return data
    except ValueError as error:
        raise SmokeFailure(f"{label}: response is not JSON ({excerpt})") from error


def generate_test_png() -> tuple[bytes, str, str]:
    """Build a standalone 32x32 test PNG in memory if no image file is provided."""
    width = height = 32
    raw = b"".join(b"\x00" + b"\x5a\x9e\xd6" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return png, "image/png", "smoke_test_image.png"


def load_image(image_path: Path | None) -> tuple[bytes, str, str]:
    if image_path is not None:
        if not image_path.is_file():
            raise SmokeFailure(f"image file not found: {image_path}")
        data = image_path.read_bytes()
        suffix = image_path.suffix.lower()
        content_type = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
        return data, content_type, image_path.name

    if DEFAULT_FIXTURE_IMAGE.is_file():
        data = DEFAULT_FIXTURE_IMAGE.read_bytes()
        return data, "image/jpeg", DEFAULT_FIXTURE_IMAGE.name

    return generate_test_png()


def require_endpoint_is_cloud(base_url: str, allow_local: bool) -> None:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" and not allow_local:
        raise SmokeFailure(
            f"refusing non-HTTPS base URL {base_url!r}; pass --allow-local for local dev"
        )
    host = (parsed.hostname or "").lower()
    if host in LOCAL_HOSTS and not allow_local:
        raise SmokeFailure(
            f"refusing local host {host!r}; smoke test is for deployed cloud endpoints"
        )


def step_health(client: Client, ctx: dict[str, Any]) -> str:
    status, payload = client.request("GET", "/health")
    parse_json("GET /health", status, payload)
    return "ok"


def step_upload_authorization(client: Client, ctx: dict[str, Any]) -> str:
    body = {
        "household_id": ctx["household_id"],
        "filename": ctx["filename"],
        "content_type": ctx["content_type"],
        "file_size_bytes": len(ctx["image_bytes"]),
    }
    resp = client.post_json("/api/media/upload-authorization", body)
    upload_url = resp.get("upload_url")
    image_ref = resp.get("image_ref")
    if not upload_url or not image_ref:
        raise SmokeFailure(f"upload authorization missing upload_url or image_ref: {resp}")
    ctx["upload_url"] = upload_url
    ctx["image_ref"] = image_ref
    ctx["required_headers"] = resp.get("required_headers") or {}
    return f"image_ref={image_ref[:35]}..."


def step_binary_upload(client: Client, ctx: dict[str, Any]) -> str:
    upload_url = ctx["upload_url"]
    headers = dict(ctx.get("required_headers", {}))
    headers.setdefault("Content-Type", ctx["content_type"])
    headers["Content-Length"] = str(len(ctx["image_bytes"]))

    status, payload = client.put_binary(upload_url, ctx["image_bytes"], headers=headers)
    if status not in (200, 201):
        excerpt = payload.decode("utf-8", errors="replace")[:200]
        raise SmokeFailure(f"PUT {upload_url}: expected HTTP 200 or 201, got {status}: {excerpt}")
    return f"uploaded {len(ctx['image_bytes'])} bytes (HTTP {status})"


def step_agent_vision(client: Client, ctx: dict[str, Any]) -> str:
    body = {
        "household_id": ctx["household_id"],
        "message": "這張照片裡有什麼？請辨識品項",
        "image_ref": ctx["image_ref"],
    }
    resp = client.post_json("/agent/messages", body)

    # 1. HITL Rule 8 compliance: read-only query must not create pending approval cards
    if resp.get("pending_approval") is not None:
        raise SmokeFailure("read-only vision query produced an unexpected pending_approval card")

    # 2. Check vision results
    results = resp.get("results") or []
    vision_results = [r for r in results if isinstance(r, dict) and r.get("kind") == "vision"]
    if not vision_results:
        message = str(resp.get("message", ""))[:200]
        raise SmokeFailure(
            f"/agent/messages did not return a 'vision' result item (message: {message!r}). "
            "Ensure Azure OpenAI Vision is configured on the backend."
        )

    vision_data = vision_results[0].get("data") or {}
    scene_type = vision_data.get("scene_type", "unknown")
    detected = vision_data.get("detected_items") or []
    item_names = [
        item.get("canonical_name") or item.get("name")
        for item in detected
        if isinstance(item, dict)
    ]

    agent_message = str(resp.get("message", "")).strip().replace("\n", " ")
    agent_excerpt = (agent_message[:60] + "...") if len(agent_message) > 60 else agent_message

    return f"scene={scene_type}, detected={item_names or 'none'}, reply={agent_excerpt!r}"


STEPS: list[tuple[str, Callable[[Client, dict[str, Any]], str]]] = [
    ("1 /health", step_health),
    ("2 /api/media/upload-authorization", step_upload_authorization),
    ("3 PUT binary upload", step_binary_upload),
    ("4 POST /agent/messages (Vision)", step_agent_vision),
]


def run(client: Client, ctx: dict[str, Any], out=print) -> int:
    for label, step in STEPS:
        started = time.perf_counter()
        try:
            detail = step(client, ctx)
        except SmokeFailure as failure:
            out(f"FAIL  {label}: {failure}")
            out("\nSTOP: Vision smoke test failed. Cloud Vision connectivity is NOT verified.")
            return 1
        out(f"PASS  {label} ({time.perf_counter() - started:.1f}s) {detail}")
    out(f"\nALL {len(STEPS)} VISION STEPS PASSED against {client.base_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default=os.environ.get("NESTOCK_BASE_URL", ""))
    parser.add_argument("--image", type=Path, default=None, help="path to image file (jpg/png)")
    parser.add_argument("--household", default=HOUSEHOLD_ID, help="target household ID")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per request")
    parser.add_argument("--allow-local", action="store_true", help="dev rehearsal only")
    args = parser.parse_args(argv)

    if not args.base_url:
        print("error: provide --base-url or set NESTOCK_BASE_URL", file=sys.stderr)
        return 2

    try:
        require_endpoint_is_cloud(args.base_url, args.allow_local)
        img_bytes, content_type, filename = load_image(args.image)
        ctx: dict[str, Any] = {
            "household_id": args.household,
            "image_bytes": img_bytes,
            "content_type": content_type,
            "filename": filename,
        }
    except SmokeFailure as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 2

    return run(Client(args.base_url, args.timeout), ctx)


if __name__ == "__main__":
    raise SystemExit(main())
