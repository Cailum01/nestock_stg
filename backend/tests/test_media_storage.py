import pytest
from pydantic import ValidationError

from app.models.storage import (
    AllowedMediaContentType,
    UploadAuthorizationRequest,
    UploadAuthorizationResponse,
)
from app.services.provisional_storage.interfaces import MediaStorageService
from app.services.provisional_storage.local_staging_adapter import (
    LocalStagingMediaStorage,
)
from app.services.vision import MockVisionService


def sample_request() -> dict:
    return {
        "household_id": "demo-household",
        "filename": "milk_carton.jpg",
        "content_type": AllowedMediaContentType.IMAGE_JPEG,
        "file_size_bytes": 1024 * 500,  # 500 KB
    }


def test_upload_request_model_validation():
    req = UploadAuthorizationRequest(**sample_request())
    assert req.household_id == "demo-household"
    assert req.content_type == "image/jpeg"
    assert req.file_size_bytes == 512000


@pytest.mark.parametrize(
    "invalid_type",
    ["application/pdf", "image/gif", "video/mp4", "text/plain"],
)
def test_upload_request_rejects_disallowed_content_types(invalid_type):
    with pytest.raises(ValidationError):
        UploadAuthorizationRequest(**(sample_request() | {"content_type": invalid_type}))


@pytest.mark.parametrize(
    "invalid_size",
    [0, -1, 10_485_761],  # 0, negative, > 10MB
)
def test_upload_request_rejects_invalid_file_sizes(invalid_size):
    with pytest.raises(ValidationError):
        UploadAuthorizationRequest(**(sample_request() | {"file_size_bytes": invalid_size}))


def test_upload_response_model_validation():
    resp = UploadAuthorizationResponse(
        upload_url="http://localhost:8000/staging/upload/token-123",
        image_ref="blob://households/demo-household/media/test-uuid.jpg",
        expires_in_seconds=300,
        required_headers={"Content-Type": "image/jpeg"},
    )
    assert resp.expires_in_seconds == 300
    assert resp.image_ref.startswith("blob://households/demo-household/media/")


def test_local_staging_media_storage_protocol_conformance():
    storage = LocalStagingMediaStorage()
    assert isinstance(storage, MediaStorageService)


@pytest.mark.anyio
async def test_local_staging_creates_authorization_and_validates():
    storage = LocalStagingMediaStorage()
    req = UploadAuthorizationRequest(**sample_request())

    auth_resp = await storage.create_upload_authorization(req)
    assert auth_resp.upload_url.startswith("http")
    assert auth_resp.image_ref.startswith("blob://households/demo-household/media/")
    assert auth_resp.image_ref.endswith(".jpg")
    assert auth_resp.expires_in_seconds > 0

    # Before upload simulation, media does not exist
    assert await storage.verify_media_exists("demo-household", auth_resp.image_ref) is False

    # Simulate completed client upload into staging
    await storage.simulate_client_upload(
        household_id="demo-household",
        image_ref=auth_resp.image_ref,
        payload_bytes=b"dummy_image_data",
    )

    # After simulation, media exists in staging
    assert await storage.verify_media_exists("demo-household", auth_resp.image_ref) is True

    # Cross-household access must return False
    assert await storage.verify_media_exists("other-household", auth_resp.image_ref) is False


@pytest.mark.anyio
async def test_local_staging_receive_upload_lifecycle():
    storage = LocalStagingMediaStorage(vision_service=MockVisionService())
    req = UploadAuthorizationRequest(**sample_request())
    auth_resp = await storage.create_upload_authorization(req)
    token = auth_resp.upload_url.split("/")[-1]

    # Verify not yet uploaded
    assert await storage.verify_media_exists("demo-household", auth_resp.image_ref) is False
    assert await storage.get_media_bytes("demo-household", auth_resp.image_ref) is None

    # Perform staged upload
    payload = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    household_id, image_ref = await storage.receive_staged_upload(token, payload)
    assert household_id == "demo-household"
    assert image_ref == auth_resp.image_ref

    # Verify media now exists and bytes match
    assert await storage.verify_media_exists("demo-household", auth_resp.image_ref) is True
    assert await storage.get_media_bytes("demo-household", auth_resp.image_ref) == payload


@pytest.mark.anyio
async def test_local_staging_receive_upload_invalid_or_expired_token():
    storage = LocalStagingMediaStorage()
    with pytest.raises(KeyError):
        await storage.receive_staged_upload("invalid-token", b"bytes")


@pytest.mark.anyio
async def test_local_staging_receive_upload_payload_boundary_validation():
    storage = LocalStagingMediaStorage()
    req = UploadAuthorizationRequest(**sample_request())
    auth_resp = await storage.create_upload_authorization(req)
    token = auth_resp.upload_url.split("/")[-1]

    # Empty payload
    with pytest.raises(ValueError, match="Payload cannot be empty"):
        await storage.receive_staged_upload(token, b"")

    # Exceeding 10MB limit
    with pytest.raises(ValueError, match="Payload exceeds"):
        await storage.receive_staged_upload(token, b"x" * (10_485_760 + 1))
