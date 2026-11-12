"""LOCAL TRY-OUT ONLY: chat with the Agent in the terminal.

With Cosmos configured the Agent may make a purchase card; it cannot be confirmed
here (use the App or `POST /agent/resume`), so nothing is written.

Run from backend/:  uv run python -m app.agent.chat
Needs backend/.env (see .env.example). Type 離開 or press Ctrl+C to quit.
"""

from __future__ import annotations

import asyncio
import sys

from app.agent.read_agent import get_read_agent
from app.core.config import settings
from app.models.agent import AgentRequest


async def main() -> None:
    agent = get_read_agent()
    if agent is None:
        print("還沒設定 Azure OpenAI：請把 3 個設定填進 backend/.env")
        return
    print("已連上 Azure OpenAI。輸入問題，輸入「離開」結束。\n")
    while True:
        message = input("你    ：").strip()
        if message in {"離開", "exit", "quit"}:
            break
        if not message:
            continue
        response = await agent.respond(
            AgentRequest(household_id=settings.demo_household_id, message=message)
        )
        print(f"Agent ：{response.message}\n")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print()
