import asyncio

from fastapi.testclient import TestClient

from app.agent.read_agent import get_read_agent
from app.core.config import settings
from app.core.dependencies import get_inventory_service, get_shopping_service
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
