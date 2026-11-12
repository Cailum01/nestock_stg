"""Stable inventory-item identity for approval-gated item creation."""

from uuid import NAMESPACE_URL, uuid5


def resolve_inventory_item_id(operation_id: str) -> str:
    """Derive one stable item ID from the create operation's stable identity.

    This helper does not look up or match existing inventory items.
    """
    if not operation_id or not operation_id.strip():
        raise ValueError("operation_id must not be empty.")

    return str(uuid5(NAMESPACE_URL, f"nestock:inventory-item:{operation_id}"))
