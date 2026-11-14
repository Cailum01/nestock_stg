"""Stable identities for direct Shopping Intent retries.

The request identity is scoped to a household and operation kind. Callers must
reuse the same request_id after timeouts or lost responses; changing the payload
under that identity is rejected by B1's durable receipt fingerprint.
"""

import hashlib


def resolve_shopping_intent_identity(*, household_id: str, request_id: str) -> tuple[str, str]:
    """Return deterministic operation and Shopping item IDs for one add request."""
    scope = household_id.strip()
    request = request_id.strip()
    if not scope:
        raise ValueError("household_id must not be empty.")
    if not request:
        raise ValueError("request_id must not be empty.")

    def stable_id(kind: str) -> str:
        identity = f"shopping-intent-add\0{kind}\0{scope}\0{request}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"shopping-intent-{kind}-{digest}"

    return stable_id("operation"), stable_id("item")
