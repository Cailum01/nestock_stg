"""Focused Google default-household tests requiring the Firestore emulator."""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest

from app.models.identity import HouseholdRole
from app.services.firestore_identity import FirestoreIdentityRepository
from app.services.google_identity import GoogleIdentityRateLimited, GoogleIdentityService
from app.services.identity import hash_secret

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
FIRESTORE_EMULATOR = bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))


def _client():
    from google.cloud import firestore

    return firestore.Client(project=f"nestock-test-{uuid.uuid4().hex}")


@pytest.mark.skipif(not FIRESTORE_EMULATOR, reason="requires FIRESTORE_EMULATOR_HOST")
def test_google_identity_default_household_is_atomic_and_reused_per_account():
    client = _client()
    repository = FirestoreIdentityRepository(client)
    first_subject = f"google-subject-{uuid.uuid4().hex}"
    second_subject = f"google-subject-{uuid.uuid4().hex}"
    audience = "test-web-client"
    subjects = {
        "first-account-token": first_subject,
        "second-account-token": second_subject,
    }

    def verify(token: str, expected_audience: str) -> dict[str, object]:
        return {
            "iss": "accounts.google.com",
            "aud": expected_audience,
            "exp": datetime.now(UTC).timestamp() + 60,
            "sub": subjects[token],
        }

    service = GoogleIdentityService(repository, audience=audience, verifier=verify)

    async def execute() -> None:
        first, second = await asyncio.gather(
            service.authenticate("first-account-token"),
            service.authenticate("first-account-token"),
        )
        first_device, first_bearer, first_household = first
        second_device, second_bearer, second_household = second
        assert first_device != second_device
        assert first_bearer != second_bearer
        assert first_household == second_household

        third_device, third_bearer, third_household = await service.authenticate(
            "first-account-token"
        )
        assert third_device not in {first_device, second_device}
        assert third_bearer not in {first_bearer, second_bearer}
        assert third_household == first_household
        other_device, other_bearer, other_household = await service.authenticate(
            "second-account-token"
        )
        assert other_device not in {first_device, second_device, third_device}
        assert other_bearer not in {first_bearer, second_bearer, third_bearer}
        assert other_household != first_household

        with pytest.raises(GoogleIdentityRateLimited):
            await service.authenticate("first-account-token")
        first_memberships = await repository.list_device_memberships(first_device)
        second_memberships = await repository.list_device_memberships(second_device)
        third_memberships = await repository.list_device_memberships(third_device)
        other_memberships = await repository.list_device_memberships(other_device)
        assert len(first_memberships.memberships) == 1
        assert len(second_memberships.memberships) == 1
        assert len(third_memberships.memberships) == 1
        assert first_memberships.memberships[0].household_id == first_household
        assert second_memberships.memberships[0].household_id == first_household
        assert third_memberships.memberships[0].household_id == first_household
        assert other_memberships.memberships[0].household_id == other_household
        assert sorted(
            [
                first_memberships.memberships[0].role,
                second_memberships.memberships[0].role,
            ]
        ) == [HouseholdRole.MEMBER, HouseholdRole.OWNER]
        assert third_memberships.memberships[0].role is HouseholdRole.MEMBER
        assert other_memberships.memberships[0].role is HouseholdRole.OWNER
        assert len(list(client.collection("households").stream())) == 2
        first_mapping = (
            client.document(f"google_identities/{hash_secret(first_subject)}").get().to_dict()
        )
        second_mapping = (
            client.document(f"google_identities/{hash_secret(second_subject)}").get().to_dict()
        )
        assert first_mapping["default_household_id"] == first_household
        assert second_mapping["default_household_id"] == other_household
        assert "sub" not in first_mapping and "email" not in first_mapping
        assert first_subject not in str(first_mapping)
        first_install = client.document(f"installations/{first_device}").get().to_dict()
        assert first_install["credential_hash"] != first_bearer
        assert first_install["default_household_id"] == first_household
        household_memberships = [
            snapshot.to_dict() for snapshot in client.collection("household_memberships").stream()
        ]
        assert (
            sum(document["household_id"] == first_household for document in household_memberships)
            == 3
        )
        assert (
            sum(document["household_id"] == other_household for document in household_memberships)
            == 1
        )
        limit = (
            client.document(f"identity_attempt_limits/google-install-{hash_secret(first_subject)}")
            .get()
            .to_dict()
        )
        assert len(limit["created_at"]) == 3

    try:
        asyncio.run(execute())
    finally:
        client.close()
