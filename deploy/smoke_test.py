"""STG smoke test: check API contracts without writing data by default.

Why it is built this way (2026-11-19): the old six-step test compared the live data with a
fixed Demo baseline and stopped at the first failure, so leftover rehearsal data failed
it, and nothing covered adding stock from a photo (Issue #206).

Layers (each check is reported on its own; one failure does not hide the rest):
  L0 version    deployed image includes the expected STG commit     (read-only)
  L1 services   /health, speech/auth, Agent, /agent/open-card       (read-only or gated)
  L2 data       invariants of /inventory, /shopping, /forecast     (read-only)
  L3 photo      photo -> vision -> approval card                    (needs --write --yes)
  L4 write      photo -> increase_stock card -> confirm -> quantity +N
                (needs --write --yes)

Rules:
- Standard library only, so it runs on a laptop with no venv. The base URL is the only
  input for unauthenticated checks; an optional bearer token is neither printed nor stored.
- No mock stands in for a real service: a mock Speech answer is a failure.
- Protected routes require an explicit install bearer token; household-scoped routes also
  require an explicit household ID. Without them, the test expects HTTP 401 and does not
  read domain data. No household is assumed.
- Agent and photo flows can create pending approval cards. They are skipped unless both
  `--write` and `--yes` are supplied. Do not run these checks unless you accept their
  external effects; an unconfirmed card is not automatically cancelled.
- L4 changes the explicitly selected household: it adds to one item and leaves the new
  quantity and one `adjustment` transaction behind. It does not undo them.
- Targets the cloud HTTPS endpoint. localhost / 10.0.2.2 are refused unless
  `--allow-local` is given (dev rehearsal only).

Usage:
  python3 deploy/smoke_test.py --base-url https://<host>
  python3 deploy/smoke_test.py --base-url https://<host> --bearer-token <TOKEN> --household-id <ID>
  python3 deploy/smoke_test.py --base-url https://<host> --skip L2
  python3 deploy/smoke_test.py --base-url https://<host> --bearer-token <TOKEN> \
      --household-id <ID> --write --yes
Exit code: 0 no FAIL, 1 at least one FAIL, 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIO = Path(__file__).resolve().parent / "smoke-audio-detergent.wav"
DEFAULT_IMAGE = REPO_ROOT / "backend" / "tests" / "fixtures" / "vision" / "img_single.jpg"

APP_NAME = "nestock-api"
EXPECTED_BACKEND_VERSION = "v1.0.1"
DEFAULT_EXPECTED_COMMIT = "origin/stg"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "10.0.2.2", "::1"}

# What MockSpeechToTextService returns for any clip over 64 bytes. Seeing it means the
# deployed runtime is running with NESTOCK_SPEECH_MOCK or without real Speech.
MOCK_SPEECH_TRANSCRIPT = "幫我查一下衛生紙"
# The test clip says "洗衣精還有幾瓶". A real transcription contains this word.
SPEECH_EXPECTED_WORD = "洗衣精"

AGENT_QUESTION = "衛生紙還有幾包？"

# Actions a photo card may hold (contracts/approval-lifecycle.md sections 10, 10.1).
PHOTO_CARD_ACTIONS = {"create_item", "increase_stock", "complete_purchase"}
SHOPPING_STATUSES = {"pending", "completed"}
FORECAST_STATUSES = {"estimated", "insufficient_data"}
FORECAST_ACTIONS = {"none", "consider_restock", "check_sufficiency"}
# A reference, never the bytes: anything this long in `image_ref` is not a reference.
IMAGE_REF_MAX_LENGTH = 2048

# The question that makes the Agent show the household's open card (and nothing else).
PHOTO_MESSAGE = "我買了這個，幫我加到庫存"

LAYERS = ("L0", "L1", "L2", "L3", "L4")
PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"


class SmokeFailure(Exception):
    """A check failed."""


class SmokeSkip(Exception):
    """A check did not run; the reason is part of the report."""


class SmokeWarn(Exception):
    """A check ran but found something to look at that is not a failure."""


class Client:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        bearer_token: str | None = None,
        household_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.bearer_token = bearer_token
        self.household_id = household_id

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        merged = {"Accept": "application/json"}
        if content_type:
            merged["Content-Type"] = content_type
        url = path if path.startswith("http") else self.base_url + path
        base = urllib.parse.urlsplit(self.base_url)
        target = urllib.parse.urlsplit(url)
        if (target.scheme, target.netloc) == (base.scheme, base.netloc):
            if self.bearer_token:
                merged["Authorization"] = f"Bearer {self.bearer_token}"
            if self.household_id:
                merged["X-Household-Id"] = self.household_id
        merged.update(headers or {})
        req = urllib.request.Request(url, data=body, method=method, headers=merged)
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


def require_endpoint_is_cloud(base_url: str, allow_local: bool) -> None:
    parts = urllib.parse.urlsplit(base_url)
    if parts.hostname is None:
        raise SmokeFailure(f"not a URL: {base_url!r}")
    is_local = parts.hostname in LOCAL_HOSTS or parts.hostname.endswith(".local")
    if is_local and not allow_local:
        raise SmokeFailure(
            f"{parts.hostname} is a local address; use the cloud HTTPS endpoint. "
            "Use --allow-local only for a dev rehearsal."
        )
    if parts.scheme != "https" and not (allow_local and is_local):
        raise SmokeFailure(f"base URL must be https://, got {parts.scheme}://")


def require_list(label: str, value: object) -> list:
    if not isinstance(value, list):
        raise SmokeFailure(f"{label} did not return a list")
    return value


def require_dict(label: str, value: object) -> dict:
    if not isinstance(value, dict):
        raise SmokeFailure(f"{label} did not return an object")
    return value


# -- L0 version ---------------------------------------------------------------------------


def image_tag_commit(tag: str) -> tuple[str | None, bool]:
    """`20261119114146-39358d7` -> ('39358d7', False); `...-39358d7-dirty` -> (..., True)."""
    parts = tag.split("-")
    dirty = parts[-1] == "dirty"
    if dirty:
        parts = parts[:-1]
    if len(parts) < 2 or not parts[-1] or parts[-1] == "nogit":
        return None, dirty
    return parts[-1], dirty


def judge_version(
    tag: str | None,
    behind: int | None,
    expected_commit: str = DEFAULT_EXPECTED_COMMIT,
) -> str:
    """Judge whether a deployed image includes the expected STG commit."""
    if not tag:
        raise SmokeSkip("deployed image tag unknown (az not signed in? pass --deployed-tag)")
    commit, dirty = image_tag_commit(tag)
    if commit is None:
        raise SmokeWarn(f"image tag {tag!r} carries no git commit")
    if behind is None:
        raise SmokeWarn(
            f"commit {commit} cannot be compared with {expected_commit!r} (fetch refs?)"
        )
    if behind > 0:
        raise SmokeFailure(
            f"deployed {commit} is missing {behind} commits from expected {expected_commit}; "
            "verify the intended STG source revision"
        )
    if dirty:
        raise SmokeWarn(f"deployed {commit} is up to date but was built from a dirty tree")
    return f"deployed {commit} includes expected commit {expected_commit}"


def default_az_image(app: str) -> str | None:
    """The running image of the Container App, or None when az cannot tell."""
    query = f"[?name=='{app}'].properties.template.containers[0].image | [0]"
    try:
        out = subprocess.run(
            ["az", "containerapp", "list", "--query", query, "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    lines = [line.strip() for line in out.stdout.splitlines() if "azurecr.io" in line]
    return lines[-1] if lines else None


def default_commits_behind(
    commit: str,
    expected_commit: str = DEFAULT_EXPECTED_COMMIT,
) -> int | None:
    """How many expected commits are absent from `commit`, or None."""
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "rev-list",
                "--count",
                f"{commit}..{expected_commit}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def check_version(client: Client, ctx: dict) -> str:
    tag = ctx.get("deployed_tag")
    if not tag:
        image = ctx["az_image"](ctx["app"])
        tag = image.rsplit(":", 1)[-1] if image else None
    behind = None
    if tag:
        commit, _ = image_tag_commit(tag)
        if commit:
            behind = ctx["commits_behind"](commit, ctx["expected_commit"])
    return judge_version(tag, behind, ctx["expected_commit"])


# -- L1 services --------------------------------------------------------------------------


def check_health(client: Client, ctx: dict) -> str:
    body = client.get_json("/health")
    if (
        not isinstance(body, dict)
        or body.get("status") != "ok"
        or body.get("version") != EXPECTED_BACKEND_VERSION
    ):
        raise SmokeFailure(
            f"/health returned {body!r}, expected status='ok' and "
            f"version={EXPECTED_BACKEND_VERSION!r}"
        )
    return f"status ok ({EXPECTED_BACKEND_VERSION})"


def unauthenticated_probe(
    client: Client,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
) -> str | None:
    """Return a pass detail after verifying 401, or None when credentials are supplied."""
    if client.bearer_token:
        return None
    status, payload = client.request(method, path, body=body, content_type=content_type)
    if status != 401:
        raise SmokeFailure(
            f"{method} {path}: expected HTTP 401 without a bearer token, got HTTP {status}"
        )
    return "unauthenticated request correctly returned HTTP 401; scoped check skipped"


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
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def check_speech(client: Client, ctx: dict) -> str:
    if not client.bearer_token:
        body, content_type = multipart(
            {"language": "zh-TW"}, "file", "auth-check.wav", b"", "audio/wav"
        )
        unauthenticated = unauthenticated_probe(
            client,
            "/api/speech/transcribe",
            method="POST",
            body=body,
            content_type=content_type,
        )
        if unauthenticated:
            return unauthenticated
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


def check_agent(client: Client, ctx: dict) -> str:
    if not ctx.get("write"):
        raise SmokeSkip("Agent messages may create approval cards; add --write --yes to run")
    if not client.bearer_token or not client.household_id:
        raise SmokeFailure("Agent check requires --bearer-token and --household-id")
    result = require_dict(
        "/agent/messages",
        client.post_json(
            "/agent/messages",
            {"household_id": client.household_id, "message": AGENT_QUESTION},
        ),
    )
    message = str(result.get("message", "")).strip()
    if not message:
        raise SmokeFailure("the Agent answered with an empty message")
    return f"answered ({len(message)} characters)"


def open_card_route(client: Client) -> dict:
    """GET /agent/open-card (PR #215): the household's newest unconfirmed card. Read-only,
    does not call the model."""
    household_id = client.household_id or "smoke-auth-check"
    unauthenticated = unauthenticated_probe(
        client, f"/agent/open-card?household_id={urllib.parse.quote(household_id)}"
    )
    if unauthenticated:
        return {"message": "", "results": [], "pending_approval": None}
    result = require_dict(
        "/agent/open-card",
        client.get_json(f"/agent/open-card?household_id={urllib.parse.quote(household_id)}"),
    )
    missing = [key for key in ("message", "results", "pending_approval") if key not in result]
    if missing:
        raise SmokeFailure(f"/agent/open-card lacks {missing}")
    card = result["pending_approval"]
    if card is not None and (not isinstance(card, dict) or not card.get("approval_id")):
        raise SmokeFailure("/agent/open-card returned a card without an approval_id")
    return result


def check_open_card(client: Client, ctx: dict) -> str:
    if not client.bearer_token:
        open_card_route(client)
        return "unauthenticated request correctly returned HTTP 401; card check skipped"
    card = open_card_route(client)["pending_approval"]
    return f"card {card['approval_id']} is open" if card else "no card is open"


# -- L2 data invariants -------------------------------------------------------------------


def inventory_problems(rows: list, household_id: str) -> list[str]:
    problems: list[str] = []
    seen: set[object] = set()
    for row in rows:
        if not isinstance(row, dict):
            problems.append("a row is not an object")
            continue
        item_id = row.get("item_id")
        if item_id in seen:
            problems.append(f"duplicate item_id {item_id}")
        seen.add(item_id)
        if row.get("household_id") != household_id:
            problems.append(f"{item_id} is not in {household_id}")
        for field in ("item_id", "name", "unit"):
            if not isinstance(row.get(field), str) or not row.get(field):
                problems.append(f"{item_id}.{field} missing")
        quantity = row.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            problems.append(f"{item_id}.quantity is {quantity!r}, expected an integer >= 0")
        if "category" in row:
            problems.append(f"{item_id} has a category (inventory has none)")
        image_ref = row.get("image_ref")
        if image_ref is not None and (
            not isinstance(image_ref, str)
            or image_ref.startswith("data:")
            or len(image_ref) > IMAGE_REF_MAX_LENGTH
        ):
            problems.append(f"{item_id}.image_ref is not an opaque reference")
    return problems


def shopping_problems(rows: list, household_id: str) -> list[str]:
    problems: list[str] = []
    seen: set[object] = set()
    for row in rows:
        if not isinstance(row, dict):
            problems.append("a row is not an object")
            continue
        key = row.get("shopping_item_id")
        if key in seen:
            problems.append(f"duplicate shopping_item_id {key}")
        seen.add(key)
        if row.get("household_id") != household_id:
            problems.append(f"{key} is not in {household_id}")
        if row.get("status") not in SHOPPING_STATUSES:
            problems.append(f"{key}.status is {row.get('status')!r}")
        for field in ("price", "unit_price", "total_price"):
            if field in row:
                problems.append(f"{key} stores {field} (product price is never persisted)")
    return problems


def forecast_problems(rows: list, inventory: list) -> list[str]:
    quantities = {r.get("item_id"): r.get("quantity") for r in inventory if isinstance(r, dict)}
    problems: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            problems.append("a row is not an object")
            continue
        item_id = row.get("item_id")
        if row.get("status") not in FORECAST_STATUSES:
            problems.append(f"{item_id}.status is {row.get('status')!r}")
        if row.get("suggested_action") not in FORECAST_ACTIONS:
            problems.append(f"{item_id}.suggested_action is {row.get('suggested_action')!r}")
        if item_id in quantities and row.get("confirmed_quantity") != quantities[item_id]:
            problems.append(f"{item_id}.confirmed_quantity differs from the inventory quantity")
    return problems


def check_inventory(client: Client, ctx: dict) -> str:
    unauthenticated = unauthenticated_probe(client, "/inventory")
    if unauthenticated:
        return unauthenticated
    rows = require_list("/inventory", client.get_json("/inventory"))
    problems = inventory_problems(rows, client.household_id or "")
    if problems:
        raise SmokeFailure("inventory: " + "; ".join(problems))
    return f"{len(rows)} items satisfy the invariants"


def check_shopping(client: Client, ctx: dict) -> str:
    unauthenticated = unauthenticated_probe(client, "/shopping")
    if unauthenticated:
        return unauthenticated
    rows = require_list("/shopping", client.get_json("/shopping"))
    problems = shopping_problems(rows, client.household_id or "")
    if problems:
        raise SmokeFailure("shopping: " + "; ".join(problems))
    return f"{len(rows)} rows satisfy the invariants"


def check_forecast(client: Client, ctx: dict) -> str:
    unauthenticated = unauthenticated_probe(client, "/forecast")
    if unauthenticated:
        return unauthenticated
    rows = require_list("/forecast", client.get_json("/forecast"))
    inventory = require_list("/inventory", client.get_json("/inventory"))
    problems = forecast_problems(rows, inventory)
    if problems:
        raise SmokeFailure("forecast: " + "; ".join(problems))
    return f"{len(rows)} forecasts; none changes a confirmed quantity"


# -- cards and photos (L3, L4) ------------------------------------------------------------


def inventory_quantity(client: Client, item_id: str) -> int | None:
    rows = require_list("/inventory", client.get_json("/inventory"))
    for row in rows:
        if isinstance(row, dict) and row.get("item_id") == item_id:
            return row.get("quantity")
    return None


def inventory_snapshot(client: Client) -> dict[str, int | None]:
    rows = require_list("/inventory", client.get_json("/inventory"))
    return {r["item_id"]: r.get("quantity") for r in rows if isinstance(r, dict) and "item_id" in r}


def photo_message(client: Client, ctx: dict) -> dict:
    """Upload the test photo and send it to the Agent. Returns the Agent response."""
    image_path: Path = ctx["image"]
    try:
        image = image_path.read_bytes()
    except OSError as error:
        raise SmokeFailure(f"test photo unreadable: {image_path} ({error})") from error
    auth = require_dict(
        "/api/media/upload-authorization",
        client.post_json(
            "/api/media/upload-authorization",
            {
                "household_id": client.household_id,
                "filename": image_path.name,
                "content_type": "image/jpeg",
                "file_size_bytes": len(image),
            },
        ),
    )
    upload_url, image_ref = auth.get("upload_url"), auth.get("image_ref")
    if not upload_url or not image_ref:
        raise SmokeFailure(f"upload authorization lacks upload_url or image_ref: {auth}")
    headers = dict(auth.get("required_headers") or {})
    headers.setdefault("Content-Type", "image/jpeg")
    status, payload = client.request("PUT", upload_url, body=image, headers=headers)
    if status not in (200, 201):
        raise SmokeFailure(
            f"PUT upload: expected HTTP 200 or 201, got {status} {_excerpt(payload)}"
        )
    return require_dict(
        "/agent/messages",
        client.post_json(
            "/agent/messages",
            {
                "household_id": client.household_id,
                "message": PHOTO_MESSAGE,
                "image_ref": image_ref,
            },
        ),
    )


def resume(client: Client, approval_id: str, approved: bool) -> dict:
    return require_dict(
        "/agent/resume",
        client.post_json(
            "/agent/resume",
            {
                "household_id": client.household_id,
                "approval_id": approval_id,
                "approved": approved,
            },
        ),
    )


def outcome_of(response: dict) -> str | None:
    for item in response.get("results") or []:
        if isinstance(item, dict) and item.get("kind") == "action_result":
            data = item.get("data")
            if isinstance(data, dict):
                return data.get("outcome")
    return None


def photo_card_problems(response: dict, card: dict, quantities: dict[str, int | None]) -> list[str]:
    problems: list[str] = []
    if not any(
        isinstance(r, dict) and r.get("kind") == "vision" for r in response.get("results") or []
    ):
        problems.append("no vision result in the response")
    operations = card.get("operations")
    if not isinstance(operations, list) or not operations:
        return problems + ["the card has no operations"]
    for op in operations:
        action = op.get("action") if isinstance(op, dict) else None
        if action not in PHOTO_CARD_ACTIONS:
            problems.append(f"unexpected card action {action!r}")
        if not isinstance(op, dict) or not op.get("operation_id"):
            problems.append("an operation has no operation_id")
            continue
        if action == "increase_stock":
            values = op.get("values") or {}
            added = values.get("quantity_added")
            if isinstance(added, bool) or not isinstance(added, int) or added < 1:
                problems.append(f"increase_stock quantity_added is {added!r}, expected >= 1")
            before = values.get("inventory_quantity_before")
            actual = quantities.get(values.get("item_id"))
            if before != actual:
                problems.append(
                    f"increase_stock says the quantity is {before!r}, inventory says {actual!r}"
                )
    return problems


def no_card_failure(client: Client, response: dict) -> SmokeFailure:
    detail = (
        "the photo produced no approval card; Agent said: " + str(response.get("message", ""))[:160]
    )
    try:
        stored = open_card_route(client)["pending_approval"]
    except SmokeFailure:
        stored = None
    if stored:
        detail += f". BUT card {stored.get('approval_id')} is open (stored; the response hid it)"
    return SmokeFailure(detail)


def check_photo(client: Client, ctx: dict) -> str:
    if not ctx.get("write"):
        raise SmokeSkip("photo upload can create an approval card; add --write --yes to run")
    if not client.bearer_token or not client.household_id:
        raise SmokeFailure("photo check requires --bearer-token and --household-id")
    # The snapshot is taken BEFORE the photo: a backend that writes while it builds the card
    # must not be able to hide behind a baseline read afterwards.
    snapshot = inventory_snapshot(client)
    response = photo_message(client, ctx)
    card = response.get("pending_approval")
    if not isinstance(card, dict):
        raise no_card_failure(client, response)
    problems = photo_card_problems(response, card, snapshot)
    for item_id, before in snapshot.items():
        after = inventory_quantity(client, item_id)
        if after != before:
            problems.append(f"{item_id} changed from {before} to {after} without a confirmation")
    if problems:
        raise SmokeFailure("photo card: " + "; ".join(problems))
    actions = sorted({op.get("action") for op in card["operations"]})
    return f"card is well formed ({', '.join(actions)}); no quantity changed; card left to expire"


def check_write(client: Client, ctx: dict) -> str:
    if not ctx.get("write"):
        raise SmokeSkip("writes real data; add --write --yes to run it")
    if not client.bearer_token or not client.household_id:
        raise SmokeFailure("write check requires --bearer-token and --household-id")
    # A card left open (by L3 or an earlier run) is returned again for the same photo, so
    # this works with or without one. Its shape is checked before anything is confirmed.
    snapshot = inventory_snapshot(client)
    response = photo_message(client, ctx)
    card = response.get("pending_approval")
    if not isinstance(card, dict):
        raise no_card_failure(client, response)
    operations = card.get("operations")
    if (
        not isinstance(operations, list)
        or len(operations) != 1
        or not isinstance(operations[0], dict)
        or operations[0].get("action") != "increase_stock"
    ):
        actions = (
            [op.get("action") if isinstance(op, dict) else None for op in operations]
            if isinstance(operations, list)
            else repr(operations)
        )
        raise SmokeFailure(
            "L4 needs a photo of ONE item the household already has; the card holds "
            f"{actions}. Nothing was confirmed; the card is "
            "left open (confirm or cancel it in the App, or wait 30 minutes)"
        )
    op = operations[0]
    operation_id = op.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise SmokeFailure("L4 card has no operation_id; nothing was confirmed")
    values = op.get("values")
    if not isinstance(values, dict):
        raise SmokeFailure("L4 increase_stock values are malformed; nothing was confirmed")
    item_id = values.get("item_id")
    if not isinstance(item_id, str) or not item_id or item_id not in snapshot:
        raise SmokeFailure("L4 card has no matching inventory item; nothing was confirmed")
    added = values.get("quantity_added")
    if isinstance(added, bool) or not isinstance(added, int) or added < 1:
        raise SmokeFailure(f"L4 quantity_added is {added!r}, expected a positive integer")
    before = values.get("inventory_quantity_before")
    actual = snapshot[item_id]
    if (
        isinstance(before, bool)
        or not isinstance(before, int)
        or before < 0
        or isinstance(actual, bool)
        or not isinstance(actual, int)
        or actual < 0
        or before != actual
    ):
        raise SmokeFailure(
            f"L4 card says {before!r} before, inventory says {actual!r}; not confirmed"
        )

    approval_id = card.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise SmokeFailure("L4 card has no approval_id; nothing was confirmed")
    first = resume(client, approval_id, True)
    if outcome_of(first) != "completed":
        raise SmokeFailure(f"confirming gave outcome {outcome_of(first)!r}, expected 'completed'")
    after = inventory_quantity(client, item_id)
    if after != before + added:
        raise SmokeFailure(f"after confirming, quantity is {after}, expected {before + added}")

    again = resume(client, approval_id, True)
    if outcome_of(again) != "already_completed":
        raise SmokeFailure(
            f"confirming the same card again gave outcome {outcome_of(again)!r}, "
            "expected 'already_completed'"
        )
    if inventory_quantity(client, item_id) != after:
        raise SmokeFailure("confirming the same card again changed the quantity (duplicate write)")
    return (
        f"{values.get('name')} {before} -> {after} (+{added}); same card again wrote nothing. "
        "Left behind: that quantity and one adjustment transaction"
    )


CHECKS: list[tuple[str, str, Callable[[Client, dict], str]]] = [
    ("L0", "deployed version", check_version),
    ("L1", "/health", check_health),
    ("L1", "speech transcribe", check_speech),
    ("L1", "agent answers", check_agent),
    ("L1", "/agent/open-card", check_open_card),
    ("L2", "/inventory invariants", check_inventory),
    ("L2", "/shopping invariants", check_shopping),
    ("L2", "/forecast invariants", check_forecast),
    ("L3", "photo -> approval card", check_photo),
    ("L4", "photo -> confirm -> +N", check_write),
]


def run_check(
    layer: str, name: str, check: Callable[[Client, dict], str], client: Client, ctx: dict
) -> tuple[str, str, str, str, float]:
    started = time.perf_counter()
    try:
        status, detail = PASS, check(client, ctx)
    except SmokeFailure as failure:
        status, detail = FAIL, str(failure)
    except SmokeSkip as skip:
        status, detail = SKIP, str(skip)
    except SmokeWarn as warn:
        status, detail = WARN, str(warn)
    except Exception as error:  # noqa: BLE001 - report this check; keep running independent checks
        status, detail = FAIL, f"unexpected {type(error).__name__}: {error}"
    return layer, name, status, detail, time.perf_counter() - started


def run(
    client: Client,
    ctx: dict,
    layers: set[str] | None = None,
    skipped: set[str] | None = None,
    out=print,
) -> int:
    selected = [
        check
        for check in CHECKS
        if (layers is None or check[0] in layers) and check[0] not in (skipped or set())
    ]
    results = []
    for layer, name, check in selected:
        result = run_check(layer, name, check, client, ctx)
        results.append(result)
        out(f"{result[2]:<5} {layer} {name} ({result[4]:.1f}s) {result[3]}")
    counts = {s: sum(1 for r in results if r[2] == s) for s in (PASS, FAIL, WARN, SKIP)}
    out(
        f"\n{counts[PASS]} passed, {counts[FAIL]} failed, {counts[WARN]} warnings, "
        f"{counts[SKIP]} skipped against {client.base_url}"
    )
    if counts[FAIL]:
        out("At least one check failed. A skipped check is not a pass.")
    return 1 if counts[FAIL] else 0


def parse_layers(text: str) -> set[str]:
    chosen = {part.strip().upper() for part in text.split(",") if part.strip()}
    unknown = chosen - set(LAYERS)
    if unknown:
        raise SmokeFailure(f"unknown layer {', '.join(sorted(unknown))}; use {', '.join(LAYERS)}")
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default=os.environ.get("NESTOCK_BASE_URL", ""))
    parser.add_argument("--audio", type=Path, default=DEFAULT_AUDIO)
    parser.add_argument(
        "--image", type=Path, default=DEFAULT_IMAGE, help="photo of ONE household item"
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per request")
    parser.add_argument("--allow-local", action="store_true", help="dev rehearsal only")
    parser.add_argument("--only", default="", help="comma list of layers to run, e.g. L1,L3")
    parser.add_argument("--skip", default="", help="comma list of layers to skip, e.g. L2")
    parser.add_argument(
        "--bearer-token",
        default="",
        help="install bearer token for protected checks (not printed)",
    )
    parser.add_argument(
        "--household-id",
        default="",
        help="authorized household for scoped checks; required with --bearer-token",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="enable Agent/photo approval-card flows and L4 data writes",
    )
    parser.add_argument("--yes", action="store_true", help="confirm --write side effects")
    parser.add_argument("--deployed-tag", default="", help="image tag; otherwise read with az")
    parser.add_argument(
        "--expected-commit",
        default=DEFAULT_EXPECTED_COMMIT,
        help="STG commit/ref the deployed image must include (default: origin/stg)",
    )
    parser.add_argument("--app", default=APP_NAME, help="Container App name for L0")
    args = parser.parse_args(argv)

    if not args.base_url:
        print("error: give --base-url or set NESTOCK_BASE_URL", file=sys.stderr)
        return 2
    if args.write and not args.yes:
        print("error: --write enables approval-card/data side effects; add --yes", file=sys.stderr)
        return 2
    if args.yes and not args.write:
        print("error: --yes requires --write", file=sys.stderr)
        return 2
    if bool(args.bearer_token) != bool(args.household_id):
        print("error: --bearer-token and --household-id must be supplied together", file=sys.stderr)
        return 2
    if args.write and not args.bearer_token:
        print("error: --write --yes requires --bearer-token and --household-id", file=sys.stderr)
        return 2
    try:
        require_endpoint_is_cloud(args.base_url, args.allow_local)
        layers = parse_layers(args.only) if args.only else None
        skipped = parse_layers(args.skip) if args.skip else set()
    except SmokeFailure as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 2
    if layers is None:
        layers = set(LAYERS) if args.write else {"L0", "L1", "L2", "L3"}
    ctx = {
        "audio": args.audio,
        "image": args.image,
        "write": args.write and args.yes,
        "expected_commit": args.expected_commit,
        "deployed_tag": args.deployed_tag or None,
        "app": args.app,
        "az_image": default_az_image,
        "commits_behind": default_commits_behind,
    }
    return run(
        Client(
            args.base_url,
            args.timeout,
            bearer_token=args.bearer_token or None,
            household_id=args.household_id or None,
        ),
        ctx,
        layers,
        skipped,
    )


if __name__ == "__main__":
    raise SystemExit(main())
