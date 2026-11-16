"""B2 Demo-day smoke test (checklist §6). Baseline reset is B1's loader (PR #131)."""

import importlib.util
import json
import threading
from copy import deepcopy
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


def inventory_row(item_id, name, quantity, unit, location):
    return {
        "item_id": item_id,
        "household_id": "demo-household",
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "created_at": STAMP,
        "updated_at": STAMP,
        "location": location,
    }


def shopping_row(slug, name, quantity, unit, item_id):
    return {
        "shopping_item_id": f"seed-shopping-{slug}",
        "household_id": "demo-household",
        "name": name,
        "quantity": quantity,
        "unit": unit,
        "status": "pending",
        "created_at": STAMP,
        "updated_at": STAMP,
        "item_id": item_id,
    }


MANIFEST = {
    "household_id": "demo-household",
    "inventory": [
        inventory_row("seed-detergent", "洗衣精", 1, "瓶", "陽台洗衣機旁"),
        inventory_row("seed-tissue", "衛生紙", 8, "包", "浴室櫃子"),
        inventory_row("seed-flour", "麵粉", 0, "包", "廚房食品櫃"),
    ],
    "shopping": [
        shopping_row("detergent", "洗衣精", 2, "瓶", "seed-detergent"),
        shopping_row("flour", "麵粉", 1, "包", "seed-flour"),
        shopping_row("toothpaste", "牙膏", 1, "條", None),
    ],
    "transactions": [
        {
            "transaction_id": "seed-tx-detergent-2026-10-01",
            "household_id": "demo-household",
            "item_id": "seed-detergent",
            "operation_id": "seed-op-detergent-2026-10-01",
            "quantity_delta": 2,
            "reason": "purchase",
            "created_at": "2026-10-01T04:00:00Z",
        }
    ],
}


# -- smoke test against a fake backend ---------------------------------------------------


class Backend:
    """Serves the baseline; tests mutate `state` to break one thing at a time."""

    def __init__(self):
        self.state = {
            "inventory": deepcopy(MANIFEST["inventory"]),
            "shopping": deepcopy(MANIFEST["shopping"]),
            "forecast": [
                {
                    "item_id": "seed-detergent",
                    "status": "estimated",
                    "suggested_action": "consider_restock",
                    "days_until_empty": 3.0,
                    "confirmed_quantity": 1,
                },
                {
                    "item_id": "seed-tissue",
                    "status": "estimated",
                    "suggested_action": "none",
                    "days_until_empty": 40.0,
                    "confirmed_quantity": 8,
                },
                {
                    "item_id": "seed-flour",
                    "status": "estimated",
                    "suggested_action": "consider_restock",
                    "days_until_empty": 0.0,
                    "confirmed_quantity": 0,
                },
            ],
            "transcript": "洗衣精還有幾瓶",
            "agent": {
                "message": "衛生紙還有 8 包，放在浴室櫃子。",
                "results": [],
                "pending_approval": None,
            },
        }
        self.requests = []
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, document):
                payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                backend.requests.append(self.path)
                routes = {
                    "/health": {"status": "ok"},
                    "/inventory": backend.state["inventory"],
                    "/shopping": backend.state["shopping"],
                    "/forecast": backend.state["forecast"],
                }
                self.reply(routes[self.path])

            def do_POST(self):
                backend.requests.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/api/speech/transcribe":
                    self.reply({"transcript": backend.state["transcript"]})
                else:
                    self.reply(backend.state["agent"])

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def backend():
    server = Backend()
    yield server
    server.close()


def run_smoke(backend, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF" + b"\0" * 200)
    lines = []
    code = smoke.run(
        smoke.Client(backend.url, 5),
        {"manifest": smoke.load_manifest(manifest), "audio": audio},
        out=lines.append,
    )
    return code, "\n".join(lines)


def test_smoke_passes_all_six_steps_on_the_baseline(backend, tmp_path):
    code, output = run_smoke(backend, tmp_path)

    assert code == 0, output
    assert output.count("PASS  ") == 6
    assert "FAIL" not in output


def test_smoke_stops_at_the_first_failure_and_runs_nothing_after_it(backend, tmp_path):
    backend.state["inventory"][1]["quantity"] = 7

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  2 /inventory" in output and "seed-tissue.quantity" in output
    assert "3 /shopping" not in output
    assert backend.requests == ["/health", "/inventory"]


def test_smoke_rejects_the_mock_speech_transcript(backend, tmp_path):
    backend.state["transcript"] = smoke.MOCK_SPEECH_TRANSCRIPT

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  5 speech" in output and "MOCK" in output
    assert "6 agent" not in output


def test_smoke_fails_when_forecast_says_insufficient_data(backend, tmp_path):
    backend.state["forecast"][0]["status"] = "insufficient_data"

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  4 /forecast" in output and "insufficient_data" in output


def test_smoke_fails_when_normal_stock_item_is_flagged_low(backend, tmp_path):
    backend.state["forecast"][1]["suggested_action"] = "consider_restock"

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "seed-tissue.suggested_action" in output


def test_smoke_fails_on_an_unexpected_pending_shopping_row(backend, tmp_path):
    backend.state["shopping"].append(shopping_row("tissue", "衛生紙", 1, "包", "seed-tissue"))

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  3 /shopping" in output and "seed-shopping-tissue" in output


def test_smoke_fails_on_an_extra_inventory_item(backend, tmp_path):
    backend.state["inventory"].append(inventory_row("extra-item", "多餘", 1, "個", "客廳"))

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  2 /inventory" in output and "extra-item" in output


def test_smoke_fails_on_a_duplicate_inventory_item(backend, tmp_path):
    backend.state["inventory"].append(dict(backend.state["inventory"][0]))

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  2 /inventory" in output and "duplicate" in output


def test_smoke_fails_on_an_inventory_item_from_another_household(backend, tmp_path):
    other = inventory_row("other-house", "別戶", 1, "個", "別處")
    other["household_id"] = "other-household"
    backend.state["inventory"].append(other)

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  2 /inventory" in output and "other-house" in output


def test_smoke_fails_on_an_extra_completed_shopping_row(backend, tmp_path):
    done = shopping_row("tissue", "衛生紙", 1, "包", "seed-tissue")
    done["status"] = "completed"
    backend.state["shopping"].append(done)

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  3 /shopping" in output and "seed-shopping-tissue" in output


def test_smoke_fails_when_a_baseline_shopping_row_is_already_completed(backend, tmp_path):
    backend.state["shopping"][0]["status"] = "completed"

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  3 /shopping" in output and "expected 'pending'" in output


def test_smoke_fails_when_an_unlinked_baseline_shopping_row_is_missing(backend, tmp_path):
    backend.state["shopping"] = [
        r for r in backend.state["shopping"] if r["shopping_item_id"] != "seed-shopping-toothpaste"
    ]

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  3 /shopping" in output and "seed-shopping-toothpaste" in output


def test_smoke_fails_when_agent_answer_lacks_the_baseline_facts(backend, tmp_path):
    backend.state["agent"]["message"] = "目前查不到相關資料。"

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  6 agent" in output


def test_smoke_fails_when_the_agent_answer_has_a_wrong_quantity(backend, tmp_path):
    backend.state["agent"]["message"] = "衛生紙剩 3 包，放在浴室櫃子，共 18 項。"

    code, output = run_smoke(backend, tmp_path)

    assert code == 1
    assert "FAIL  6 agent" in output


def test_smoke_refuses_a_local_address_without_the_rehearsal_flag():
    with pytest.raises(smoke.SmokeFailure, match="local address"):
        smoke.require_endpoint_is_cloud("https://localhost:8000", allow_local=False)
    with pytest.raises(smoke.SmokeFailure, match="local address"):
        smoke.require_endpoint_is_cloud("https://10.0.2.2:8000", allow_local=False)
    with pytest.raises(smoke.SmokeFailure, match="https"):
        smoke.require_endpoint_is_cloud("http://demo.example.com", allow_local=False)
    smoke.require_endpoint_is_cloud("https://demo.example.com", allow_local=False)


def test_smoke_reports_an_unreachable_server_as_a_failure(tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
    lines = []

    code = smoke.run(
        smoke.Client("http://127.0.0.1:1", 2),
        {"manifest": MANIFEST, "audio": tmp_path / "x.wav"},
        out=lines.append,
    )

    assert code == 1
    assert "cannot reach the server" in "\n".join(lines)


# ---------------------------------------------------------------------------
# deploy-aca.sh: image-pull credential selection (stubbed `az`, no cloud access)
# ---------------------------------------------------------------------------

import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402

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
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AZ_LOG": str(log),
        "ROLE_OUT": role_out,
        "ROLE_RC": str(role_rc),
        "LIST_RC": str(list_rc),
        "PULL_SECRET": PULL_SECRET,
    }
    result = subprocess.run(
        ["bash", str(DEPLOY / "deploy-aca.sh")],
        env=env,
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
