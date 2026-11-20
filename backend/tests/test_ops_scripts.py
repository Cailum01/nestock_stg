"""B2 smoke test and Demo-day scripts (stubbed `az`, fake backend; no cloud access)."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = load_script("smoke_test")

STAMP = "2026-11-12T04:00:00Z"
HOUSE = "demo-household"
MOUSE = "item-mouse"


def inventory_row(item_id, name, quantity, unit="個"):
    return {
        "item_id": item_id,
        "household_id": HOUSE,
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def shopping_row(key, name, status="pending"):
    return {
        "shopping_item_id": key,
        "household_id": HOUSE,
        "name": name,
        "quantity": 1,
        "unit": "個",
        "status": status,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


# -- smoke test against a fake backend ---------------------------------------------------


class Backend:
    """A stateful fake of the deployed API. Tests flip one switch in `state` at a time."""

    def __init__(self):
        self.state = {
            "inventory": [
                inventory_row(MOUSE, "滑鼠", 4),
                inventory_row("item-tissue", "衛生紙", 8),
            ],
            "shopping": [shopping_row("s1", "牙膏")],
            "transcript": "洗衣精還有幾瓶",
            "agent_message": "衛生紙還有 8 包。",
            "photo": "increase",  # increase | create | none | stray
            "card": None,  # the open card
            "completed": {},  # approval_id -> card
            "before_override": None,  # make the card lie about the quantity
            "write_on_photo": False,  # a bug: the photo itself changes the quantity
            "duplicate_on_retry": False,  # a bug: confirming twice writes twice
            "cancel_writes": False,  # a bug: cancelling still adds
        }
        self.requests = []
        self.responses = []
        self.request_headers = []
        self.counter = 0
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, document, status=200):
                backend.responses.append((self.path, status))
                payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def body(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try:
                    return json.loads(raw)
                except ValueError:
                    return {}

            def do_GET(self):
                backend.requests.append(self.path)
                backend.request_headers.append(
                    (
                        self.path,
                        self.headers.get("Authorization"),
                        self.headers.get("X-Household-Id"),
                    )
                )
                if self.path != "/health" and not self.headers.get("Authorization"):
                    return self.reply({"detail": "bearer required"}, 401)
                s = backend.state
                forecast = [
                    {
                        "item_id": r["item_id"],
                        "status": "estimated",
                        "suggested_action": "none",
                        "confirmed_quantity": r["quantity"],
                    }
                    for r in s["inventory"]
                ]
                routes = {
                    "/health": {"status": "ok"},
                    "/inventory": s["inventory"],
                    "/shopping": s["shopping"],
                    "/forecast": s.get("forecast", forecast),
                }
                if self.path.startswith("/agent/open-card"):
                    if s.get("open_card_broken"):
                        return self.reply({"message": ""})
                    self.reply({"message": "", "results": [], "pending_approval": s["card"]})
                    return
                self.reply(routes[self.path])

            def do_PUT(self):
                backend.requests.append(self.path)
                backend.request_headers.append(
                    (
                        self.path,
                        self.headers.get("Authorization"),
                        self.headers.get("X-Household-Id"),
                    )
                )
                if not self.headers.get("Authorization"):
                    return self.reply({"detail": "bearer required"}, 401)
                self.body()
                self.reply({}, 201)

            def do_POST(self):
                backend.requests.append(self.path)
                backend.request_headers.append(
                    (
                        self.path,
                        self.headers.get("Authorization"),
                        self.headers.get("X-Household-Id"),
                    )
                )
                document = self.body()
                if not self.headers.get("Authorization"):
                    return self.reply({"detail": "bearer required"}, 401)
                if self.path == "/api/speech/transcribe":
                    self.reply({"transcript": backend.state["transcript"]})
                elif self.path == "/api/media/upload-authorization":
                    self.reply(
                        {"upload_url": backend.url + "/upload", "image_ref": "blob://x/1.jpg"}
                    )
                elif self.path == "/agent/resume":
                    self.reply(backend.resume(document))
                else:
                    self.reply(backend.message(document))

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()

    def quantity(self, item_id):
        return next(r for r in self.state["inventory"] if r["item_id"] == item_id)

    def make_card(self):
        self.counter += 1
        item = self.quantity(MOUSE)
        if self.state["photo"] == "create":
            ops = [
                {
                    "operation_id": f"op{self.counter}",
                    "action": "create_item",
                    "values": {"name": "新東西", "quantity": 1},
                }
            ]
        else:
            before = self.state["before_override"]
            ops = [
                {
                    "operation_id": f"op{self.counter}",
                    "action": "increase_stock",
                    "values": {
                        "item_id": MOUSE,
                        "name": "滑鼠",
                        "quantity_added": 1,
                        "inventory_quantity_before": item["quantity"] if before is None else before,
                    },
                }
            ]
        return {"approval_id": f"ap{self.counter}", "operations": ops}

    def message(self, document):
        s = self.state
        answer = {"message": s["agent_message"], "results": [], "pending_approval": None}
        if "image_ref" in document:
            if s["card"]:
                answer["message"] = "先前的確認卡還沒確認"
                answer["pending_approval"] = s["card"]
                return answer
            if s["photo"] == "none":
                answer["message"] = "辨識不出來"
                return answer
            s["card"] = self.make_card()
            if s["write_on_photo"]:
                self.quantity(MOUSE)["quantity"] += 1
            answer["results"] = [{"kind": "vision", "data": {}}]
            if s["photo"] == "stray":
                answer["message"] = "這個問題我查了太多次還是答不出來"
            else:
                answer["pending_approval"] = s["card"]
        return answer

    def resume(self, document):
        s = self.state
        reply = {
            "message": "",
            "results": [{"kind": "action_result", "data": {}}],
            "pending_approval": None,
        }
        data = reply["results"][0]["data"]
        approval_id = document["approval_id"]
        if not document["approved"]:
            if s["cancel_writes"]:
                self.quantity(MOUSE)["quantity"] += 1
            if s["card"] and s["card"]["approval_id"] == approval_id:
                s["card"] = None
            data["outcome"] = "cancelled"
        elif approval_id in s["completed"]:
            if s["duplicate_on_retry"]:
                self.quantity(MOUSE)["quantity"] += 1
            data["outcome"] = "already_completed"
        elif s["card"] and s["card"]["approval_id"] == approval_id:
            for op in s["card"]["operations"]:
                if op["action"] == "increase_stock":
                    self.quantity(MOUSE)["quantity"] += op["values"]["quantity_added"]
            s["completed"][approval_id] = s["card"]
            s["card"] = None
            data["outcome"] = "completed"
        else:
            data["outcome"] = "not_found"
        return reply


@pytest.fixture
def backend():
    server = Backend()
    yield server
    server.close()


def make_ctx(tmp_path, **extra):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 200)
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"\xff\xd8" + b"\0" * 100)
    ctx = {
        "audio": audio,
        "image": image,
        "write": False,
        "expected_commit": "origin/stg",
        "deployed_tag": None,
        "app": "nestock-api",
        "az_image": lambda app: None,
        "commits_behind": lambda commit, expected_commit: 0,
    }
    ctx.update(extra)
    return ctx


def run_smoke(backend, tmp_path, layers=None, skipped=None, **extra):
    authenticated = extra.pop("authenticated", True)
    lines = []
    code = smoke.run(
        smoke.Client(
            backend.url,
            5,
            bearer_token="test-token" if authenticated else None,
            household_id=HOUSE if authenticated else None,
        ),
        make_ctx(tmp_path, **extra),
        layers,
        skipped,
        out=lines.append,
    )
    return code, "\n".join(lines)


def test_smoke_passes_every_read_only_layer_on_a_healthy_backend(backend, tmp_path):
    code, output = run_smoke(backend, tmp_path, deployed_tag="20261119114146-39358d7")

    assert code == 0, output
    assert "FAIL" not in output
    assert "PASS  L0" in output and "SKIP  L3" in output
    assert "SKIP  L4" in output  # the writing check never runs by accident
    assert backend.quantity(MOUSE)["quantity"] == 4
    assert backend.state["card"] is None
    assert "/agent/messages" not in backend.requests


def test_default_unauthenticated_run_expects_401_and_leaves_no_cards_or_data(backend, tmp_path):
    code, output = run_smoke(backend, tmp_path, authenticated=False)

    assert code == 0, output
    assert "HTTP 401" in output
    assert backend.state["card"] is None
    assert backend.quantity(MOUSE)["quantity"] == 4
    assert "/agent/messages" not in backend.requests
    assert "/api/media/upload-authorization" not in backend.requests
    assert "/upload" not in backend.requests
    assert "/agent/resume" not in backend.requests
    protected = {"/inventory", "/shopping", "/forecast", "/api/speech/transcribe"}
    assert all(
        status == 401
        for path, status in backend.responses
        if path.split("?", 1)[0] in protected or path.startswith("/agent/open-card")
    )


def test_explicit_credentials_are_sent_to_protected_routes(backend, tmp_path):
    code, output = run_smoke(backend, tmp_path, layers={"L2"})

    assert code == 0, output
    assert backend.request_headers
    assert all(token == "Bearer test-token" for _, token, _ in backend.request_headers)
    assert all(household == HOUSE for _, _, household in backend.request_headers)


def test_client_does_not_send_api_credentials_to_external_upload_urls(monkeypatch):
    observed = {}

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b""

    def fake_urlopen(request, timeout):
        observed.update(request.header_items())
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(smoke.urllib.request, "urlopen", fake_urlopen)
    client = smoke.Client(
        "https://api.example.test",
        5,
        bearer_token="sensitive-test-token",
        household_id=HOUSE,
    )

    status, payload = client.request("PUT", "https://storage.example.test/signed-upload")

    assert status == 201 and payload == b""
    header_names = {name.lower() for name in observed}
    assert "authorization" not in header_names
    assert "x-household-id" not in header_names


def test_smoke_reports_every_check_and_does_not_stop_at_the_first_failure(backend, tmp_path):
    backend.state["inventory"][0]["category"] = "3C"

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  L2 /inventory" in output and "category" in output
    assert "PASS  L2 /shopping" in output and "SKIP  L3" in output


def test_open_card_route_is_checked_and_reports_the_open_card(backend, tmp_path):
    code, output = run_smoke(backend, tmp_path, layers={"L1"})
    assert code == 0, output
    assert "PASS  L1 /agent/open-card" in output and "no card is open" in output

    backend.state["card"] = {"approval_id": "open-one", "operations": []}
    code, output = run_smoke(backend, tmp_path, layers={"L1"})
    assert code == 0 and "card open-one is open" in output
    assert "/agent/resume" not in backend.requests


def test_open_card_route_with_the_wrong_shape_fails(backend, tmp_path):
    backend.state["open_card_broken"] = True

    code, output = run_smoke(backend, tmp_path, layers={"L1"})

    assert code == 1
    assert "FAIL  L1 /agent/open-card" in output and "lacks" in output


def test_smoke_rejects_the_mock_speech_transcript(backend, tmp_path):
    backend.state["transcript"] = smoke.MOCK_SPEECH_TRANSCRIPT

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  L1 speech" in output and "MOCK" in output


def test_smoke_fails_when_the_agent_answers_with_nothing(backend, tmp_path):
    backend.state["agent_message"] = "  "

    code, output = run_smoke(backend, tmp_path, layers={"L1"}, write=True)

    assert code == 1
    assert "FAIL  L1 agent" in output


def test_smoke_only_and_skip_choose_the_layers(backend, tmp_path):
    _, only = run_smoke(backend, tmp_path, layers={"L2"})
    _, skipped = run_smoke(backend, tmp_path, layers={"L1", "L2"}, skipped={"L2"})

    assert "L2" in only and "L1" not in only
    assert "L1" in skipped and "L2" not in skipped


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda s: (
                s["inventory"].append(inventory_row("dup", "x", 1))
                or s["inventory"].append(inventory_row("dup", "x", 1))
            ),
            "duplicate item_id dup",
        ),
        (
            lambda s: s["inventory"].__setitem__(0, {**s["inventory"][0], "quantity": -1}),
            "quantity",
        ),
        (
            lambda s: s["inventory"].__setitem__(0, {**s["inventory"][0], "household_id": "other"}),
            "not in demo-household",
        ),
        (
            lambda s: s["inventory"].__setitem__(
                0, {**s["inventory"][0], "image_ref": "data:image/jpeg;base64,AAAA"}
            ),
            "image_ref",
        ),
        (
            lambda s: s["inventory"].__setitem__(0, {**s["inventory"][0], "image_ref": "A" * 3000}),
            "image_ref",
        ),
    ],
    ids=["duplicate", "negative", "other-household", "data-uri", "too-long"],
)
def test_inventory_invariants_catch_each_violation(backend, tmp_path, mutate, expected):
    mutate(backend.state)

    code, output = run_smoke(backend, tmp_path, layers={"L2"})

    assert code == 1
    assert "FAIL  L2 /inventory" in output and expected in output


def test_shopping_invariants_reject_a_stored_price_and_an_unknown_status(backend, tmp_path):
    backend.state["shopping"][0]["price"] = 99
    backend.state["shopping"].append(shopping_row("s2", "洗衣精", status="ordered"))

    code, output = run_smoke(backend, tmp_path, layers={"L2"})

    assert code == 1
    assert "stores price" in output and "status is 'ordered'" in output


def test_forecast_must_not_change_a_confirmed_quantity(backend, tmp_path):
    backend.state["forecast"] = [
        {
            "item_id": MOUSE,
            "status": "estimated",
            "suggested_action": "none",
            "confirmed_quantity": 2,
        }
    ]

    code, output = run_smoke(backend, tmp_path, layers={"L2"})

    assert code == 1
    assert "FAIL  L2 /forecast" in output and "confirmed_quantity" in output


# -- L0 version -----------------------------------------------------------------------------


def test_image_tag_commit_reads_the_sha_and_the_dirty_marker():
    assert smoke.image_tag_commit("20261119114146-39358d7") == ("39358d7", False)
    assert smoke.image_tag_commit("20261119114146-39358d7-dirty") == ("39358d7", True)
    assert smoke.image_tag_commit("20261119114146-nogit") == (None, False)
    assert smoke.image_tag_commit("latest") == (None, False)


def test_version_verdicts():
    assert "includes expected commit origin/stg" in smoke.judge_version("20261119-39358d7", 0)
    with pytest.raises(smoke.SmokeFailure, match="missing 43 commits"):
        smoke.judge_version("20261118-718ad2d", 43, "expected-stg-sha")
    with pytest.raises(smoke.SmokeWarn, match="dirty"):
        smoke.judge_version("20261119-39358d7-dirty", 0)
    with pytest.raises(smoke.SmokeWarn, match="cannot be compared"):
        smoke.judge_version("20261119-39358d7", None)
    with pytest.raises(smoke.SmokeSkip, match="unknown"):
        smoke.judge_version(None, None)


def test_the_version_check_uses_the_az_image_when_no_tag_is_given(backend, tmp_path):
    code, output = run_smoke(
        backend,
        tmp_path,
        layers={"L0"},
        az_image=lambda app: "reg.azurecr.io/nestock-api:20261118-718ad2d",
        commits_behind=lambda commit, expected: 43 if commit == "718ad2d" else 0,
    )

    assert code == 1
    assert "FAIL  L0" in output and "718ad2d is missing 43 commits" in output


def test_version_comparison_uses_the_expected_stg_commit(monkeypatch):
    observed = {}

    def fake_run(command, **_kwargs):
        observed["command"] = command
        return type("Result", (), {"returncode": 0, "stdout": "2"})()

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)

    assert smoke.default_commits_behind("deployed-sha", "expected-stg-sha") == 2
    assert observed["command"][-1] == "deployed-sha..expected-stg-sha"
    assert "origin/main" not in observed["command"][-1]


# -- L3 photo -> card -----------------------------------------------------------------------


def test_photo_check_passes_and_leaves_the_card_open(backend, tmp_path):
    code, output = run_smoke(backend, tmp_path, layers={"L3"}, write=True)

    assert code == 0, output
    assert "PASS  L3" in output and "left to expire" in output
    assert backend.state["card"] is not None
    assert backend.quantity(MOUSE)["quantity"] == 4
    assert "/agent/resume" not in backend.requests


def test_photo_check_fails_when_the_card_lies_about_the_quantity(backend, tmp_path):
    backend.state["before_override"] = 99

    code, output = run_smoke(backend, tmp_path, layers={"L3"}, write=True)

    assert code == 1
    assert "FAIL  L3" in output and "says the quantity is 99" in output


def test_photo_check_fails_when_the_photo_alone_changes_a_quantity(backend, tmp_path):
    backend.state["write_on_photo"] = True

    code, output = run_smoke(backend, tmp_path, layers={"L3"}, write=True)

    assert code == 1
    assert "without a confirmation" in output


def test_photo_check_fails_when_the_response_shows_no_card(backend, tmp_path):
    """The 2026-11-19 cloud finding: the Agent said it gave up although a card was stored."""
    backend.state["photo"] = "stray"

    code, output = run_smoke(backend, tmp_path, layers={"L3"}, write=True)

    assert code == 1
    assert "produced no approval card" in output
    assert "is open (stored; the response hid it)" in output
    assert "/agent/resume" not in backend.requests


def test_photo_check_fails_when_a_photo_gives_no_card(backend, tmp_path):
    backend.state["photo"] = "none"

    code, output = run_smoke(backend, tmp_path, layers={"L3"}, write=True)

    assert code == 1
    assert "produced no approval card" in output


# -- L4 write ---------------------------------------------------------------------------------


def test_write_check_is_skipped_without_the_write_flag(backend, tmp_path):
    code, output = run_smoke(backend, tmp_path, layers={"L4"})

    assert code == 0
    assert "SKIP  L4" in output and "--write --yes" in output
    assert backend.quantity(MOUSE)["quantity"] == 4
    assert "/agent/resume" not in backend.requests


def test_write_check_confirms_once_and_the_same_card_again_writes_nothing(backend, tmp_path):
    code, output = run_smoke(backend, tmp_path, layers={"L4"}, write=True)

    assert code == 0, output
    assert "PASS  L4" in output and "4 -> 5 (+1)" in output
    assert backend.quantity(MOUSE)["quantity"] == 5
    assert backend.state["card"] is None


def test_write_check_confirms_the_card_an_earlier_photo_left_open(backend, tmp_path):
    code, output = run_smoke(backend, tmp_path, layers={"L3", "L4"}, write=True)

    assert code == 0, output
    assert backend.quantity(MOUSE)["quantity"] == 5


def test_write_check_catches_a_duplicate_write_on_retry(backend, tmp_path):
    backend.state["duplicate_on_retry"] = True

    code, output = run_smoke(backend, tmp_path, layers={"L4"}, write=True)

    assert code == 1
    assert "FAIL  L4" in output and "duplicate write" in output


def test_write_check_fails_without_a_card_and_confirms_nothing(backend, tmp_path):
    backend.state["photo"] = "stray"

    code, output = run_smoke(backend, tmp_path, layers={"L4"}, write=True)

    assert code == 1
    assert "produced no approval card" in output
    assert "/agent/resume" not in backend.requests


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("operation_id", None, "operation_id"),
        ("approval_id", None, "approval_id"),
        ("quantity_added", True, "positive integer"),
        ("item_id", None, "matching inventory item"),
        ("inventory_quantity_before", None, "inventory says"),
        ("inventory_quantity_before", 3, "inventory says"),
    ],
)
def test_write_check_refuses_malformed_increase_card(
    field, value, message, backend, tmp_path, monkeypatch
):
    operation = {
        "operation_id": "op1",
        "action": "increase_stock",
        "values": {
            "item_id": MOUSE,
            "quantity_added": 1,
            "inventory_quantity_before": 4,
        },
    }
    approval_id = "ap1"
    if field == "operation_id":
        operation[field] = value
    elif field == "approval_id":
        approval_id = value
    else:
        operation["values"][field] = value
    monkeypatch.setattr(
        smoke,
        "photo_message",
        lambda *_args: {
            "message": "",
            "results": [{"kind": "vision", "data": {}}],
            "pending_approval": {"approval_id": approval_id, "operations": [operation]},
        },
    )

    code, output = run_smoke(backend, tmp_path, layers={"L4"}, write=True)

    assert code == 1
    assert "FAIL  L4" in output and message in output
    assert "/agent/resume" not in backend.requests
    assert backend.quantity(MOUSE)["quantity"] == 4


def test_write_check_refuses_a_photo_of_a_new_item_and_confirms_nothing(backend, tmp_path):
    backend.state["photo"] = "create"

    code, output = run_smoke(backend, tmp_path, layers={"L4"}, write=True)

    assert code == 1
    assert "ONE item the household already has" in output
    assert backend.quantity(MOUSE)["quantity"] == 4
    assert "/agent/resume" not in backend.requests


# -- command line -----------------------------------------------------------------------------


def test_smoke_refuses_a_local_address_without_the_rehearsal_flag():
    with pytest.raises(smoke.SmokeFailure, match="local address"):
        smoke.require_endpoint_is_cloud("https://localhost:8000", allow_local=False)
    with pytest.raises(smoke.SmokeFailure, match="local address"):
        smoke.require_endpoint_is_cloud("https://10.0.2.2:8000", allow_local=False)
    with pytest.raises(smoke.SmokeFailure, match="https"):
        smoke.require_endpoint_is_cloud("http://demo.example.com", allow_local=False)
    smoke.require_endpoint_is_cloud("https://demo.example.com", allow_local=False)


def test_write_without_yes_is_a_usage_error():
    assert smoke.main(["--base-url", "https://demo.example.com", "--write"]) == 2


def test_write_with_yes_requires_explicit_authentication():
    assert smoke.main(["--base-url", "https://demo.example.com", "--write", "--yes"]) == 2


def test_bearer_and_household_must_be_supplied_together():
    assert (
        smoke.main(["--base-url", "https://demo.example.com", "--bearer-token", "test-token"]) == 2
    )


def test_write_opt_in_does_not_override_an_explicit_layer_selection(backend, capsys):
    code = smoke.main(
        [
            "--base-url",
            backend.url,
            "--allow-local",
            "--only",
            "L2",
            "--write",
            "--yes",
            "--bearer-token",
            "test-token",
            "--household-id",
            HOUSE,
        ]
    )

    assert code == 0
    assert "L4" not in capsys.readouterr().out
    assert backend.state["card"] is None
    assert backend.quantity(MOUSE)["quantity"] == 4
    assert "/agent/messages" not in backend.requests
    assert "/agent/resume" not in backend.requests


def test_an_unknown_layer_is_a_usage_error():
    assert smoke.main(["--base-url", "https://demo.example.com", "--only", "L9"]) == 2


def test_a_local_address_is_a_usage_error_without_the_flag():
    assert smoke.main(["--base-url", "https://localhost:8000"]) == 2


def test_smoke_reports_an_unreachable_server_as_a_failure(tmp_path):
    lines = []

    code = smoke.run(
        smoke.Client("http://127.0.0.1:1", 2),
        make_ctx(tmp_path),
        {"L1"},
        out=lines.append,
    )

    assert code == 1
    assert "cannot reach the server" in "\n".join(lines)


# ---------------------------------------------------------------------------
# deploy-aca.sh: image-pull credential selection (stubbed `az`, no cloud access)
# ---------------------------------------------------------------------------

import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402


def _find_bash():
    configured = os.environ.get("NESTOCK_TEST_BASH")
    if configured:
        return configured
    git_executable = shutil.which("git")
    if git_executable:
        git_bash = Path(git_executable).resolve().parents[1] / "bin" / "bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    return shutil.which("bash") or "bash"


BASH_EXE = _find_bash()

PULL_SECRET = "pull-secret-xyz"


AZ_STUB = r"""#!/usr/bin/env bash
echo "az $*" >> "$AZ_LOG"
args="$*"
case "$args" in
  "account list"*) echo sub-1 ;;
  "containerapp list"*)
    if [ "${LIST_RC:-0}" != 0 ]; then echo "AADSTS700082 expired" >&2; exit 1; fi
    echo "WARNING: The behavior of this command has been altered by an extension" >&2
    echo rg-1 ;;
  "acr list"*) echo nestockacrtest ;;
  "acr show"*"--query id"*) echo /subscriptions/sub-1/acr ;;
  "containerapp identity assign"*) echo principal-1 ;;
  "role assignment create"*) echo "pathconv=${MSYS_NO_PATHCONV:-unset}" >> "$AZ_LOG"
    echo "$ROLE_OUT" >&2; exit "${ROLE_RC:-0}" ;;
  "containerapp show"*latestRevisionName*) echo rev-1 ;;
  "containerapp show"*latestReadyRevisionName*) echo rev-1 ;;
  "containerapp show"*fqdn*) echo app.example ;;
  "acr token show"*) exit 1 ;;
  "acr token credential generate"*) echo "$PULL_SECRET" ;;
esac
exit 0
"""


def run_deploy(tmp_path, role_out="", role_rc=0, list_rc=0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "az": AZ_STUB,
        "rsync": "#!/usr/bin/env bash\nexit 0\n",
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
        "curl": '#!/usr/bin/env bash\necho \'{"status":"ok"}\'\n',
    }.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "az.log"
    log.write_text("")
    commit = subprocess.run(
        ["git", "-C", str(DEPLOY.parent), "rev-parse", "--short=7", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    bash_env = tmp_path / "bash_env.sh"
    bash_env.write_text(
        'git() { case "$*" in *"rev-parse --short=7 HEAD"*) '
        'echo "$GIT_COMMIT" ;; *"status --porcelain -- backend prompts deploy"*) '
        "return 0 ;; *) return 1 ;; esac; }\n"
        "sleep() { :; }\n"
        "rsync() { :; }\n"
        'curl() { printf \'{"status":"ok"}\\n\'; }\n',
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": os.pathsep.join((str(bin_dir), os.environ["PATH"])),
        "BASH_ENV": str(bash_env),
        "AZ_LOG": str(log),
        "GIT_COMMIT": commit,
        "ROLE_OUT": role_out,
        "ROLE_RC": str(role_rc),
        "LIST_RC": str(list_rc),
        "PULL_SECRET": PULL_SECRET,
    }
    result = subprocess.run(
        [BASH_EXE, "deploy-aca.sh"],
        env=env,
        cwd=DEPLOY,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, log.read_text()


def test_deploy_uses_identity_when_acrpull_is_granted(tmp_path):
    result, calls = run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--identity system" in calls
    assert "acr token" not in calls


def test_deploy_treats_an_existing_acrpull_assignment_as_success(tmp_path):
    result, calls = run_deploy(tmp_path, "RoleAssignmentExists: already exists", 1)
    assert result.returncode == 0, result.stderr
    assert "--identity system" in calls
    assert "acr token" not in calls


def test_deploy_falls_back_to_a_pull_only_token_when_role_assignment_is_denied(tmp_path):
    denied = "(AuthorizationFailed) roleAssignments/write over scope ... ABAC condition"
    result, calls = run_deploy(tmp_path, denied, 1)
    assert result.returncode == 0, result.stderr
    # Scope: pull of nestock-api only, 30-day credential.
    assert "--repository nestock-api content/read metadata/read" in calls
    assert "content/write" not in calls and "content/delete" not in calls
    assert "--expiration-in-days 30" in calls
    # The app gets the token, not the identity.
    assert "--username nestock-api-pull --password " + PULL_SECRET in calls
    assert "--identity system" not in calls


def test_deploy_never_enables_admin_or_prints_the_pull_password(tmp_path):
    denied = "(AuthorizationFailed) does not have authorization"
    result, calls = run_deploy(tmp_path, denied, 1)
    assert result.returncode == 0, result.stderr
    assert "--admin-enabled true" not in calls
    assert "acr credential show" not in calls
    assert PULL_SECRET not in result.stdout
    assert PULL_SECRET not in result.stderr


def test_deploy_stops_on_other_role_assignment_errors(tmp_path):
    result, calls = run_deploy(tmp_path, "(Throttled) try again later", 1)
    assert result.returncode == 1
    assert "AcrPull assignment failed" in result.stderr
    assert "acr token" not in calls
    assert "registry set" not in calls


def test_deploy_reports_a_failed_app_lookup_instead_of_assuming_a_first_deployment(tmp_path):
    result, calls = run_deploy(tmp_path, list_rc=1)
    assert result.returncode == 1
    assert "Could not list Container Apps" in result.stderr
    assert "AADSTS700082" in result.stderr
    assert "acr build" not in calls and "group list" not in calls


def test_deploy_disables_msys_path_conversion_for_az_arguments(tmp_path):
    # Git Bash rewrites "/subscriptions/..." scopes; az then reports MissingSubscription.
    result, calls = run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "pathconv=1" in calls


def test_deploy_stops_on_missing_subscription_instead_of_using_a_token(tmp_path):
    denied = "MissingSubscription: the request did not have a subscription"
    result, calls = run_deploy(tmp_path, denied, 1)
    assert result.returncode == 1
    assert "AcrPull assignment failed" in result.stderr
    assert "acr token" not in calls


def test_deploy_tags_the_image_with_the_git_commit(tmp_path):
    result, calls = run_deploy(tmp_path)
    commit = subprocess.run(
        ["git", "-C", str(DEPLOY.parent), "rev-parse", "--short=7", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert result.returncode == 0, result.stderr
    build = next(line for line in calls.splitlines() if line.startswith("az acr build"))
    tag = build.split("-t nestock-api:")[1].split()[0]
    assert re.fullmatch(r"\d{14}-" + commit + r"(-dirty)?", tag), tag


def test_deploy_ignores_az_warning_lines_when_discovering_the_resource_group(tmp_path):
    result, calls = run_deploy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "resource group rg-1\n" in result.stdout
    assert "WARNING" not in result.stdout.split("resource group")[1].splitlines()[0]
