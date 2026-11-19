"""Authentication helpers for legacy API integration tests."""

from fastapi.testclient import TestClient

from app.core.dependencies import (
    AuthorizedHousehold,
    get_authorized_household,
    get_identity_service,
    get_required_identity_service,
)
from app.models.identity import DeviceRecord, HouseholdMembership, HouseholdRole
from app.services.identity import IdentityService, hash_secret

TEST_DEVICE_ID = "test-install"
TEST_CREDENTIAL = "test-install-credential-for-api-tests-only"


class StaticIdentityRepository:
    """Narrow read-only identity fake for route tests outside identity lifecycle."""

    def __init__(self, household_id: str) -> None:
        self.household_id = household_id
        self.device = DeviceRecord(
            device_id=TEST_DEVICE_ID,
            credential_hash=hash_secret(TEST_CREDENTIAL),
            created_at="2026-11-19T00:00:00Z",
        )

    async def find_active_device_by_credential_hash(self, credential_hash):
        return self.device if credential_hash == self.device.credential_hash else None

    async def get_active_membership(self, *, device_id, household_id):
        if device_id != TEST_DEVICE_ID or household_id != self.household_id:
            return None
        return HouseholdMembership(
            device_id=device_id,
            household_id=household_id,
            role=HouseholdRole.OWNER,
            created_at="2026-11-19T00:00:00Z",
        )


def authenticated_client(app, household_id: str = "demo-household") -> TestClient:
    repository = StaticIdentityRepository(household_id)
    service = IdentityService(repository)  # Test-only repository; never runtime composition.
    app.dependency_overrides[get_identity_service] = lambda: service
    return _BearerTestClient(app, household_id)


class _BearerTestClient(TestClient):
    def __init__(self, app, household_id: str) -> None:
        super().__init__(app)
        self._identity_headers = {
            "Authorization": f"Bearer {TEST_CREDENTIAL}",
            "X-Household-Id": household_id,
        }

    def request(self, method, url, **kwargs):
        headers = dict(self._identity_headers)
        headers.update(kwargs.pop("headers", {}) or {})
        return super().request(method, url, headers=headers, **kwargs)


class _UnusedRepository:
    pass


def install_scope_override(app, household_id: str = "demo-household") -> None:
    """Bypass auth only for legacy approval-service tests focused on HITL behavior."""
    device = DeviceRecord(
        device_id=TEST_DEVICE_ID,
        credential_hash=hash_secret(TEST_CREDENTIAL),
        created_at="2026-11-19T00:00:00Z",
    )
    membership = HouseholdMembership(
        device_id=TEST_DEVICE_ID,
        household_id=household_id,
        role=HouseholdRole.OWNER,
        created_at="2026-11-19T00:00:00Z",
    )
    app.dependency_overrides[get_authorized_household] = lambda: AuthorizedHousehold(
        device=device,
        membership=membership,
    )
    app.dependency_overrides[get_required_identity_service] = lambda: IdentityService(
        _UnusedRepository()
    )
