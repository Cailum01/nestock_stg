"""Unit tests for deploy/smoke_vision.py using a lightweight fake HTTP server."""

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


smoke_vision = load_script("smoke_vision")


class FakeVisionBackend:
    def __init__(self):
        self.state = {
            "upload_auth": {
                "upload_url": "http://127.0.0.1:9999/staging/upload/token-123",
                "image_ref": "blob://households/demo-household/media/token-123.jpg",
                "expires_in_seconds": 300,
                "required_headers": {"x-ms-blob-type": "BlockBlob"},
            },
            "put_status": 201,
            "agent_response": {
                "message": "照片中有無糖高纖豆漿 1 瓶",
                "results": [
                    {
                        "kind": "vision",
                        "summary": "無糖高纖豆漿 1 瓶",
                        "data": {
                            "scene_type": "single_item",
                            "detected_items": [
                                {
                                    "canonical_name": "無糖高纖豆漿",
                                    "quantity": 1,
                                    "unit": "瓶",
                                }
                            ],
                        },
                    }
                ],
                "pending_approval": None,
            },
        }
        self.requests = []
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, document, status=200):
                payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                backend.requests.append(("GET", self.path))
                if self.path == "/health":
                    self.reply({"status": "ok"})
                else:
                    self.reply({"error": "not found"}, status=404)

            def do_POST(self):
                backend.requests.append(("POST", self.path))
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                if self.path == "/api/media/upload-authorization":
                    self.reply(backend.state["upload_auth"])
                elif self.path == "/agent/messages":
                    self.reply(backend.state["agent_response"])
                else:
                    self.reply({"error": "not found"}, status=404)

            def do_PUT(self):
                backend.requests.append(("PUT", self.path))
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(backend.state["put_status"])
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        port = self.server.server_port
        self.base_url = f"http://127.0.0.1:{port}"
        self.state["upload_auth"]["upload_url"] = f"{self.base_url}/staging/upload/token-123"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_backend():
    backend = FakeVisionBackend()
    try:
        yield backend
    finally:
        backend.close()


def test_smoke_vision_all_steps_pass(fake_backend):
    png_bytes, content_type, filename = smoke_vision.generate_test_png()
    ctx = {
        "household_id": "demo-household",
        "image_bytes": png_bytes,
        "content_type": content_type,
        "filename": filename,
    }
    client = smoke_vision.Client(fake_backend.base_url, timeout=5.0)
    lines = []
    code = smoke_vision.run(client, ctx, out=lines.append)

    assert code == 0
    assert any("ALL 4 VISION STEPS PASSED" in line for line in lines)
    assert any("PASS  1 /health" in line for line in lines)
    assert any("PASS  2 /api/media/upload-authorization" in line for line in lines)
    assert any("PASS  3 PUT binary upload" in line for line in lines)
    assert any("PASS  4 POST /agent/messages (Vision)" in line for line in lines)


def test_smoke_vision_stops_on_missing_vision_result(fake_backend):
    fake_backend.state["agent_response"] = {
        "message": "我看不到照片內容",
        "results": [],
        "pending_approval": None,
    }
    png_bytes, content_type, filename = smoke_vision.generate_test_png()
    ctx = {
        "household_id": "demo-household",
        "image_bytes": png_bytes,
        "content_type": content_type,
        "filename": filename,
    }
    client = smoke_vision.Client(fake_backend.base_url, timeout=5.0)
    lines = []
    code = smoke_vision.run(client, ctx, out=lines.append)

    assert code == 1
    assert any("FAIL  4 POST /agent/messages (Vision)" in line for line in lines)
    assert any("did not return a 'vision' result item" in line for line in lines)


def test_smoke_vision_stops_on_unexpected_pending_approval(fake_backend):
    fake_backend.state["agent_response"] = {
        "message": "已建立確認卡",
        "results": [{"kind": "vision", "data": {"scene_type": "single_item"}}],
        "pending_approval": {"approval_id": "test-card"},
    }
    png_bytes, content_type, filename = smoke_vision.generate_test_png()
    ctx = {
        "household_id": "demo-household",
        "image_bytes": png_bytes,
        "content_type": content_type,
        "filename": filename,
    }
    client = smoke_vision.Client(fake_backend.base_url, timeout=5.0)
    lines = []
    code = smoke_vision.run(client, ctx, out=lines.append)

    assert code == 1
    expected_msg = "read-only vision query produced an unexpected pending_approval card"
    assert any(expected_msg in line for line in lines)


def test_smoke_vision_stops_on_put_failure(fake_backend):
    fake_backend.state["put_status"] = 500
    png_bytes, content_type, filename = smoke_vision.generate_test_png()
    ctx = {
        "household_id": "demo-household",
        "image_bytes": png_bytes,
        "content_type": content_type,
        "filename": filename,
    }
    client = smoke_vision.Client(fake_backend.base_url, timeout=5.0)
    lines = []
    code = smoke_vision.run(client, ctx, out=lines.append)

    assert code == 1
    assert any("FAIL  3 PUT binary upload" in line for line in lines)


def test_smoke_vision_endpoint_guard():
    with pytest.raises(smoke_vision.SmokeFailure, match="refusing non-HTTPS"):
        smoke_vision.require_endpoint_is_cloud("http://api.example.com", allow_local=False)

    with pytest.raises(smoke_vision.SmokeFailure, match="refusing local host"):
        smoke_vision.require_endpoint_is_cloud("https://localhost:8000", allow_local=False)

    # Allowed with flag
    smoke_vision.require_endpoint_is_cloud("http://localhost:8000", allow_local=True)
    smoke_vision.require_endpoint_is_cloud(
        "https://nestock.azurecontainerapps.io", allow_local=False
    )


def test_load_image_raises_on_missing_file(tmp_path):
    missing_file = tmp_path / "does_not_exist.jpg"
    with pytest.raises(smoke_vision.SmokeFailure, match="image file not found"):
        smoke_vision.load_image(missing_file)


def test_generate_test_png():
    raw, ctype, name = smoke_vision.generate_test_png()
    assert len(raw) > 0
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert ctype == "image/png"
    assert name == "smoke_test_image.png"
