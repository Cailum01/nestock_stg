import asyncio

from fastapi.testclient import TestClient

from app.agent.read_agent import get_read_agent
from app.core.config import settings
from app.core.dependencies import (
    get_inventory_service,
    get_media_storage_service,
    get_shopping_service,
)
from app.main import create_app
from app.tools.queries import QueryTools


def test_health_and_openapi():
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}
        schema = client.get("/openapi.json").json()
        assert {
            "AgentRequest",
            "AgentResponse",
            "AgentResult",
            "PendingApproval",
            "ProposedOperation",
            "ResumeRequest",
            "UploadAuthorizationRequest",
            "UploadAuthorizationResponse",
        } <= schema["components"]["schemas"].keys()


def test_api_and_tools_share_services_and_scope():
    inventory = get_inventory_service()
    shopping = get_shopping_service()
    tools = QueryTools(inventory, shopping)
    with TestClient(create_app()) as client:
        for path, query in [
            ("/inventory", tools.list_inventory),
            ("/shopping", tools.list_shopping),
        ]:
            response = client.get(path)
            assert response.status_code == 200
            expected = asyncio.run(query(settings.demo_household_id))
            assert response.json() == [item.model_dump(mode="json") for item in expected]
            assert asyncio.run(query("another-household")) == []


def test_dependency_injection_reaches_api():
    class EmptyInventory:
        async def list_items(self, household_id):
            assert household_id == settings.demo_household_id
            return []

    app = create_app()
    app.dependency_overrides[get_inventory_service] = lambda: EmptyInventory()
    with TestClient(app) as client:
        assert client.get("/inventory").json() == []


def test_agent_and_repeated_resume_fail_closed_without_writes():
    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: None  # Azure OpenAI not configured
    with TestClient(app) as client:
        before = [client.get(path).json() for path in ["/inventory", "/shopping"]]
        assert (
            client.post(
                "/agent/messages",
                json={
                    "household_id": "demo-household",
                    "message": "Use one bag of rice",
                },
            ).status_code
            == 501
        )
        for approved in [True, True, False]:
            assert (
                client.post(
                    "/agent/resume",
                    json={
                        "household_id": "demo-household",
                        "approval_id": "same-approval",
                        "approved": approved,
                    },
                ).status_code
                == 501
            )
        assert before == [client.get(path).json() for path in ["/inventory", "/shopping"]]
        assert (
            client.post(
                "/agent/messages",
                json={
                    "household_id": "demo-household",
                    "message": "",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/agent/resume",
                json={
                    "household_id": "demo-household",
                    "approval_id": "x",
                    "approved": True,
                    "quantity": 999,
                },
            ).status_code
            == 422
        )


def test_media_upload_authorization_api_success():
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/media/upload-authorization",
            json={
                "household_id": "demo-household",
                "filename": "milk.jpg",
                "content_type": "image/jpeg",
                "file_size_bytes": 1024 * 500,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "upload_url" in data and data["upload_url"].startswith("http")
        assert "image_ref" in data and data["image_ref"].startswith(
            "blob://households/demo-household/media/"
        )
        assert data["expires_in_seconds"] == 300
        assert data["required_headers"]["Content-Type"] == "image/jpeg"
        assert data["required_headers"]["x-ms-blob-type"] == "BlockBlob"


def test_media_upload_authorization_api_validation_errors():
    with TestClient(create_app()) as client:
        # Invalid mime type
        assert (
            client.post(
                "/api/media/upload-authorization",
                json={
                    "household_id": "demo-household",
                    "filename": "file.gif",
                    "content_type": "image/gif",
                    "file_size_bytes": 1024,
                },
            ).status_code
            == 422
        )

        # File size exceeds 10MB limit
        assert (
            client.post(
                "/api/media/upload-authorization",
                json={
                    "household_id": "demo-household",
                    "filename": "huge.png",
                    "content_type": "image/png",
                    "file_size_bytes": 10 * 1024 * 1024 + 1,
                },
            ).status_code
            == 422
        )

        # Zero file size
        assert (
            client.post(
                "/api/media/upload-authorization",
                json={
                    "household_id": "demo-household",
                    "filename": "empty.png",
                    "content_type": "image/png",
                    "file_size_bytes": 0,
                },
            ).status_code
            == 422
        )

        # Empty household_id
        assert (
            client.post(
                "/api/media/upload-authorization",
                json={
                    "household_id": "",
                    "filename": "empty.jpg",
                    "content_type": "image/jpeg",
                    "file_size_bytes": 1024,
                },
            ).status_code
            == 422
        )


def test_media_upload_authorization_dependency_override():
    class CustomStorage:
        async def create_upload_authorization(self, request):
            from app.models.storage import UploadAuthorizationResponse

            return UploadAuthorizationResponse(
                upload_url="https://custom-storage.example.com/upload",
                image_ref=f"blob://households/{request.household_id}/media/mock.png",
                expires_in_seconds=60,
                required_headers={"x-custom": "true"},
            )

    app = create_app()
    app.dependency_overrides[get_media_storage_service] = lambda: CustomStorage()
    with TestClient(app) as client:
        response = client.post(
            "/api/media/upload-authorization",
            json={
                "household_id": "custom-household",
                "filename": "test.png",
                "content_type": "image/png",
                "file_size_bytes": 1024,
            },
        )
        assert response.status_code == 200
        assert response.json()["upload_url"] == "https://custom-storage.example.com/upload"
        assert response.json()["required_headers"] == {"x-custom": "true"}


def test_staging_binary_put_upload_flow_and_errors():
    with TestClient(create_app()) as client:
        # 1. Obtain authorization
        auth_res = client.post(
            "/api/media/upload-authorization",
            json={
                "household_id": "demo-household",
                "filename": "camera.jpg",
                "content_type": "image/jpeg",
                "file_size_bytes": 1024,
            },
        )
        assert auth_res.status_code == 200
        auth_data = auth_res.json()
        upload_url = auth_data["upload_url"]
        headers = auth_data["required_headers"]

        from urllib.parse import urlparse

        path = urlparse(upload_url).path

        # 2. Upload binary payload directly via PUT
        payload = b"\xff\xd8\xff\xe0\x00\x10JFIF_fake_image_bytes"
        put_res = client.put(path, content=payload, headers=headers)
        assert put_res.status_code == 201

        # 3. Test non-existent token returns 404
        bad_put = client.put("/staging/upload/non-existent-token", content=payload, headers=headers)
        assert bad_put.status_code == 404

        # 4. Test empty body returns 400
        empty_auth = client.post(
            "/api/media/upload-authorization",
            json={
                "household_id": "demo-household",
                "filename": "empty.jpg",
                "content_type": "image/jpeg",
                "file_size_bytes": 1024,
            },
        ).json()
        empty_path = urlparse(empty_auth["upload_url"]).path
        empty_res = client.put(empty_path, content=b"", headers=headers)
        assert empty_res.status_code == 400


def test_speech_transcribe_success_auto_language():
    fake_audio = b"\x00\x00\x00\x20ftypM4A " + b"x" * 100
    files = {"file": ("recording.m4a", fake_audio, "audio/m4a")}

    with TestClient(create_app()) as client:
        response = client.post("/api/speech/transcribe", files=files)
        assert response.status_code == 200
        data = response.json()
        assert "transcript" in data
        assert data["transcript"] == "幫我查一下衛生紙"
        assert data["detected_language"] == "zh-TW"
        assert data["duration_seconds"] is not None


def test_speech_transcribe_with_explicit_language():
    fake_audio = b"\x00\x00\x00\x20ftypM4A " + b"x" * 100
    files = {"file": ("recording.m4a", fake_audio, "audio/m4a")}
    data = {"language": "en-US"}

    with TestClient(create_app()) as client:
        response = client.post("/api/speech/transcribe", files=files, data=data)
        assert response.status_code == 200
        res_data = response.json()
        assert res_data["detected_language"] == "en-US"


def test_speech_transcribe_empty_file_returns_400():
    files = {"file": ("recording.m4a", b"", "audio/m4a")}
    with TestClient(create_app()) as client:
        response = client.post("/api/speech/transcribe", files=files)
        assert response.status_code == 400
        assert "Empty audio payload" in response.json()["detail"]


def test_speech_transcribe_oversized_file_returns_413():
    oversized = b"0" * (5 * 1024 * 1024 + 1)
    files = {"file": ("huge.m4a", oversized, "audio/m4a")}
    with TestClient(create_app()) as client:
        response = client.post("/api/speech/transcribe", files=files)
        assert response.status_code == 413


def test_speech_transcribe_unsupported_media_type_returns_415():
    files = {"file": ("fake.txt", b"plain text content", "text/plain")}
    with TestClient(create_app()) as client:
        response = client.post("/api/speech/transcribe", files=files)
        assert response.status_code == 415
