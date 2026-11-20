import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from identity_test_support import authenticated_client

from app.agent.read_agent import get_read_agent
from app.core.config import settings
from app.core.dependencies import (
    get_commerce_service,
    get_inventory_service,
    get_media_storage_service,
    get_shopping_intent_service,
    get_shopping_service,
    get_speech_service,
)
from app.main import create_app
from app.models.domain import ShoppingItem
from app.services.cosmos_write import OperationIdConflict, WriteStatus
from app.services.shopping.intents import ShoppingIntentWriteResult
from app.services.speech import MockSpeechToTextService
from app.tools.queries import QueryTools


def test_health_and_openapi():
    with authenticated_client(create_app()) as client:
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
            "AddShoppingIntentRequest",
        } <= schema["components"]["schemas"].keys()


def test_api_and_tools_share_services_and_scope():
    inventory = get_inventory_service()
    shopping = get_shopping_service()
    tools = QueryTools(inventory, shopping)
    with authenticated_client(create_app()) as client:
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
    with authenticated_client(app) as client:
        assert client.get("/inventory").json() == []


def test_direct_shopping_add_requires_auth_and_authorized_household():
    from identity_test_support import TEST_CREDENTIAL

    class ShoppingIntentServiceStub:
        def __init__(self):
            self.commands = []

        async def add(self, command):
            self.commands.append(command)
            return ShoppingIntentWriteResult(
                item=ShoppingItem(
                    shopping_item_id="manual-item",
                    household_id=command.household_id,
                    name=command.name,
                    quantity=command.quantity,
                    unit=command.unit,
                    status="pending",
                    created_at=datetime(2026, 11, 18, tzinfo=UTC),
                    updated_at=datetime(2026, 11, 18, tzinfo=UTC),
                ),
                status=WriteStatus.APPLIED,
            )

    service = ShoppingIntentServiceStub()
    app = create_app()
    app.dependency_overrides[get_shopping_intent_service] = lambda: service
    body = {"request_id": "app-add-1", "name": " 麵粉 ", "unit": " 袋 "}
    with TestClient(app) as anonymous:
        assert anonymous.post("/shopping", json=body).status_code == 401

    client = authenticated_client(app)
    with client:
        unscoped = TestClient(app).post(
            "/shopping",
            headers={"Authorization": f"Bearer {TEST_CREDENTIAL}"},
            json=body,
        )
        cross_household = client.post(
            "/shopping",
            headers={"X-Household-Id": "other-household"},
            json=body,
        )
        response = client.post("/shopping", json=body)

    assert unscoped.status_code == 400
    assert cross_household.status_code == 403
    assert response.status_code == 201
    assert response.json()["name"] == " 麵粉 "
    assert response.json()["unit"] == " 袋 "
    assert response.json()["status"] == "pending"
    assert len(service.commands) == 1
    assert service.commands[0].household_id == settings.demo_household_id


def test_direct_shopping_add_retry_is_idempotent_and_get_reloads_the_item():
    class InMemoryShoppingService:
        def __init__(self):
            self.items = {}

        async def add(self, command):
            key = (command.household_id, command.request_id)
            payload = (command.name, command.quantity, command.unit)
            existing = self.items.get(key)
            if existing is not None:
                previous_payload, item = existing
                if payload != previous_payload:
                    raise OperationIdConflict("request identity reused with different values")
                return ShoppingIntentWriteResult(item=item, status=WriteStatus.ALREADY_APPLIED)

            now = datetime(2026, 11, 18, tzinfo=UTC)
            item = ShoppingItem(
                shopping_item_id=f"manual-{command.request_id}",
                household_id=command.household_id,
                name=command.name,
                quantity=command.quantity,
                unit=command.unit,
                status="pending",
                created_at=now,
                updated_at=now,
            )
            self.items[key] = (payload, item)
            return ShoppingIntentWriteResult(item=item, status=WriteStatus.APPLIED)

        async def list_items(self, household_id):
            return [
                item
                for (item_household, _), (_, item) in self.items.items()
                if item_household == household_id
            ]

    service = InMemoryShoppingService()
    app = create_app()
    app.dependency_overrides[get_shopping_intent_service] = lambda: service
    app.dependency_overrides[get_shopping_service] = lambda: service
    client = authenticated_client(app)
    with client:
        body = {
            "request_id": "app-add-retry",
            "name": " 麵粉 ",
            "quantity": 2,
            "unit": " 袋 ",
        }
        added = client.post("/shopping", json=body)
        retried = client.post("/shopping", json=body)
        reloaded = client.get("/shopping")
        changed_retry = client.post("/shopping", json={**body, "name": "高筋麵粉"})

    assert added.status_code == 201
    assert retried.status_code == 200
    assert retried.json() == added.json()
    assert added.json()["name"] == " 麵粉 "
    assert added.json()["unit"] == " 袋 "
    assert added.json()["status"] == "pending"
    assert reloaded.status_code == 200
    assert reloaded.json() == [added.json()]
    assert changed_retry.status_code == 409
    assert len(service.items) == 1


def test_direct_shopping_add_still_validates_the_existing_request_contract():
    app = create_app()
    app.dependency_overrides[get_shopping_intent_service] = lambda: None
    with authenticated_client(app) as client:
        unavailable = client.post("/shopping", json={"request_id": "x", "name": "牛奶"})
        missing_name = client.post("/shopping", json={"request_id": "x", "name": " "})
        invalid_quantity = client.post(
            "/shopping", json={"request_id": "x", "name": "牛奶", "quantity": True}
        )
    assert unavailable.status_code == 503
    assert missing_name.status_code == 422
    assert invalid_quantity.status_code == 422


def test_agent_and_repeated_resume_fail_closed_without_writes():
    from app.core.dependencies import get_approval_service

    app = create_app()
    app.dependency_overrides[get_read_agent] = lambda: None  # Vertex project is not configured
    app.dependency_overrides[get_approval_service] = lambda: None  # Cosmos not configured
    with authenticated_client(app) as client:
        before = [client.get(path).json() for path in ["/inventory", "/shopping"]]
        unavailable = client.post(
            "/agent/messages",
            json={
                "household_id": "demo-household",
                "message": "Use one bag of rice",
            },
        )
        assert unavailable.status_code == 501
        assert "NESTOCK_VERTEX_PROJECT_ID" in unavailable.json()["detail"]
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
                == 503
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
    with authenticated_client(create_app()) as client:
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
    with authenticated_client(create_app()) as client:
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
        async def create_upload_authorization(
            self,
            request,
            *,
            base_staging_url: str | None = None,
        ):
            from app.models.storage import UploadAuthorizationResponse

            return UploadAuthorizationResponse(
                upload_url="https://custom-storage.example.com/upload",
                image_ref=f"blob://households/{request.household_id}/media/mock.png",
                expires_in_seconds=60,
                required_headers={"x-custom": "true"},
            )

    app = create_app()
    app.dependency_overrides[get_media_storage_service] = lambda: CustomStorage()
    with authenticated_client(app, "custom-household") as client:
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
    with authenticated_client(create_app()) as client:
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

    app = create_app()
    app.dependency_overrides[get_speech_service] = lambda: MockSpeechToTextService()
    with authenticated_client(app) as client:
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

    app = create_app()
    app.dependency_overrides[get_speech_service] = lambda: MockSpeechToTextService()
    with authenticated_client(app) as client:
        response = client.post("/api/speech/transcribe", files=files, data=data)
        assert response.status_code == 200
        res_data = response.json()
        assert res_data["detected_language"] == "en-US"


def test_speech_transcribe_empty_file_returns_400():
    files = {"file": ("recording.m4a", b"", "audio/m4a")}
    app = create_app()
    app.dependency_overrides[get_speech_service] = lambda: MockSpeechToTextService()
    with authenticated_client(app) as client:
        response = client.post("/api/speech/transcribe", files=files)
        assert response.status_code == 400
        assert "Empty audio payload" in response.json()["detail"]


def test_speech_transcribe_oversized_file_returns_413():
    oversized = b"0" * (5 * 1024 * 1024 + 1)
    files = {"file": ("huge.m4a", oversized, "audio/m4a")}
    app = create_app()
    app.dependency_overrides[get_speech_service] = lambda: MockSpeechToTextService()
    with authenticated_client(app) as client:
        response = client.post("/api/speech/transcribe", files=files)
        assert response.status_code == 413


def test_speech_transcribe_unsupported_media_type_returns_415():
    files = {"file": ("fake.txt", b"plain text content", "text/plain")}
    app = create_app()
    app.dependency_overrides[get_speech_service] = lambda: MockSpeechToTextService()
    with authenticated_client(app) as client:
        response = client.post("/api/speech/transcribe", files=files)
        assert response.status_code == 415


def test_commerce_and_speech_require_install_authentication():
    class Commerce:
        calls = 0

        async def search_options(self, keyword, *, limit):
            self.calls += 1
            assert keyword == "rice"
            assert limit == 3
            return []

    commerce = Commerce()
    app = create_app()
    app.dependency_overrides[get_commerce_service] = lambda: commerce
    app.dependency_overrides[get_speech_service] = lambda: MockSpeechToTextService()

    with TestClient(app) as client:
        anonymous_commerce = client.get("/api/commerce/options?keyword=rice")
        anonymous_speech = client.post(
            "/api/speech/transcribe",
            files={"file": ("sample.m4a", b"audio", "audio/m4a")},
        )
        assert anonymous_commerce.status_code == 401
        assert anonymous_speech.status_code == 401
        assert commerce.calls == 0

    with authenticated_client(app) as client:
        authenticated_commerce = client.get("/api/commerce/options?keyword=rice")
        authenticated_speech = client.post(
            "/api/speech/transcribe",
            files={"file": ("sample.m4a", b"audio", "audio/m4a")},
        )
        assert authenticated_commerce.status_code == 200
        assert authenticated_speech.status_code == 200
        assert commerce.calls == 1


def test_main_app_mounts_read_only_forecast_routes():
    # Issue #46: the shared app serves B2's forecast router (demo history without Cosmos).
    with authenticated_client(create_app()) as client:
        response = client.get("/forecast")
        assert response.status_code == 200
        assert [f["item_id"] for f in response.json()] == ["demo-rice"]
        assert client.get("/forecast/not-an-item").status_code == 404
        assert client.post("/forecast/demo-rice").status_code == 405


def test_media_upload_authorization_ignores_untrusted_host_headers(monkeypatch):
    from app.core import dependencies
    from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage

    monkeypatch.setattr(dependencies, "media_storage_service", LocalStagingMediaStorage())
    with authenticated_client(create_app()) as client:
        lan_res = client.post(
            "/api/media/upload-authorization",
            headers={
                "Host": "attacker.example",
                "X-Forwarded-Host": "attacker.example",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "203.0.113.1",
            },
            json={
                "household_id": "demo-household",
                "filename": "apple.jpg",
                "content_type": "image/jpeg",
                "file_size_bytes": 1024 * 20,
            },
        )
        assert lan_res.status_code == 200
        lan_url = lan_res.json()["upload_url"]
        assert lan_url.startswith("http://127.0.0.1:8000/staging/upload/staging-token-")

        emu_res = client.post(
            "/api/media/upload-authorization",
            headers={
                "Host": "10.0.2.2:8000",
                "X-Forwarded-Host": "10.0.2.2:8000",
                "X-Forwarded-Proto": "http",
                "X-Forwarded-For": "198.51.100.2",
            },
            json={
                "household_id": "demo-household",
                "filename": "apple.jpg",
                "content_type": "image/jpeg",
                "file_size_bytes": 1024 * 20,
            },
        )
        assert emu_res.status_code == 200
        emu_url = emu_res.json()["upload_url"]
        assert emu_url.startswith("http://127.0.0.1:8000/staging/upload/staging-token-")


def test_media_upload_authorization_env_override(monkeypatch):
    # Issue #76: NESTOCK_STAGING_BASE_URL explicitly overrides dynamic deduction
    from app.core import dependencies
    from app.services.provisional_storage.local_staging_adapter import LocalStagingMediaStorage

    custom_base = "https://upload.example.test/staging/upload"
    storage = LocalStagingMediaStorage(base_staging_url=custom_base)
    monkeypatch.setattr(dependencies, "media_storage_service", storage)

    with authenticated_client(create_app()) as client:
        res = client.post(
            "/api/media/upload-authorization",
            headers={
                "Host": "attacker.example",
                "X-Forwarded-Host": "attacker.example",
                "X-Forwarded-Proto": "http",
                "X-Forwarded-For": "203.0.113.99",
            },
            json={
                "household_id": "demo-household",
                "filename": "apple.jpg",
                "content_type": "image/jpeg",
                "file_size_bytes": 1024 * 20,
            },
        )
        assert res.status_code == 200
        assert res.json()["upload_url"].startswith(f"{custom_base}/staging-token-")
        assert res.json()["upload_url"].startswith("https://upload.example.test/")


@pytest.mark.parametrize(
    "staging_url",
    [
        None,
        "http://nestock-stg-api.example/staging/upload",
        "https://nestock-stg-api.example/other",
        "https://nestock-stg-api.example/staging/upload?next=attacker",
        "https://user:pass@nestock-stg-api.example/staging/upload",
    ],
)
def test_gcs_media_provider_requires_fixed_https_staging_url(staging_url):
    from app.core.dependencies import _create_media_storage_service

    environment = {
        "NESTOCK_MEDIA_PROVIDER": "gcs",
        "NESTOCK_DATA_PROVIDER": "firestore",
        "NESTOCK_GCS_PROJECT_ID": "test-project",
        "NESTOCK_GCS_BUCKET": "test-bucket",
    }
    if staging_url is not None:
        environment["NESTOCK_STAGING_BASE_URL"] = staging_url

    with pytest.raises(RuntimeError, match="fixed HTTPS"):
        _create_media_storage_service(
            mapping_port=object(),
            environment=environment,
        )
