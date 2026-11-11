import pytest

from app.services.inventory_item_identity import resolve_inventory_item_id


def test_same_operation_id_resolves_to_same_item_id():
    first = resolve_inventory_item_id("create-item-123")

    assert first == "71e82dd4-c1f0-5ca3-baf5-8466594cb4a2"
    assert resolve_inventory_item_id("create-item-123") == first


def test_different_operation_ids_resolve_to_different_item_ids():
    assert resolve_inventory_item_id("create-item-123") != resolve_inventory_item_id(
        "create-item-456"
    )


@pytest.mark.parametrize("operation_id", ["", " ", "\t"])
def test_blank_operation_id_is_rejected(operation_id):
    with pytest.raises(ValueError, match="operation_id must not be empty"):
        resolve_inventory_item_id(operation_id)
