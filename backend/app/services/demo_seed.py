"""Explicit, household-scoped loader for the Final Demo baseline.

Run ``python -m app.services.demo_seed --help`` from ``backend``. Commands are
read-only by default. Reset removes every document in ``demo-household`` before
reloading the manifest and therefore requires an explicit confirmation flag.
"""

import argparse
import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.domain import InventoryItem, ShoppingItem, Transaction
from app.services.cosmos_read import CosmosDocumentMapper
from app.services.cosmos_sdk import CosmosSeedContainer, create_azure_cosmos_seed_container

DEMO_HOUSEHOLD_ID = "demo-household"
_COSMOS_SYSTEM_FIELDS = frozenset({"_attachments", "_etag", "_rid", "_self", "_ts"})
_LOGGER = logging.getLogger(__name__)


class DemoSeedError(ValueError):
    """The target household does not safely match the requested seed operation."""


class DemoSeedManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    household_id: Literal["demo-household"]
    inventory: list[InventoryItem] = Field(min_length=1)
    shopping: list[ShoppingItem] = Field(default_factory=list)
    transactions: list[Transaction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_records(self) -> "DemoSeedManifest":
        inventories = {item.item_id for item in self.inventory}
        _ensure_unique((item.item_id for item in self.inventory), "Inventory item_id")
        _ensure_unique((item.shopping_item_id for item in self.shopping), "Shopping item_id")
        _ensure_unique((item.transaction_id for item in self.transactions), "Transaction id")
        _ensure_unique(
            (item.operation_id for item in self.transactions),
            "Transaction operation_id",
        )

        records = (*self.inventory, *self.shopping, *self.transactions)
        if any(record.household_id != DEMO_HOUSEHOLD_ID for record in records):
            raise ValueError("Every seed record must use household_id='demo-household'.")
        if any(item.item_id not in inventories for item in self.shopping if item.item_id):
            raise ValueError("Shopping item_id references an item outside the seed inventory.")
        if any(item.item_id not in inventories for item in self.transactions):
            raise ValueError("Transaction item_id references an item outside the seed inventory.")
        return self

    def documents(self) -> list[dict[str, object]]:
        records = (*self.inventory, *self.shopping, *self.transactions)
        documents = [CosmosDocumentMapper.to_document(record) for record in records]
        return sorted(documents, key=lambda document: str(document["id"]))


def _ensure_unique(values: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"Duplicate {label}: {value!r}.")
        seen.add(value)


@dataclass(frozen=True)
class DemoSeedResult:
    action: Literal["load", "reset", "already_loaded"]
    applied: bool
    records_planned: int
    records_created: int
    documents_deleted: int


def load_demo_seed(
    container: CosmosSeedContainer,
    manifest: DemoSeedManifest,
    *,
    apply: bool = False,
    reset: bool = False,
    confirm_household_wipe: str | None = None,
) -> DemoSeedResult:
    """Plan or apply a seed load; never update an existing document implicitly."""
    if confirm_household_wipe is not None and confirm_household_wipe != DEMO_HOUSEHOLD_ID:
        raise DemoSeedError("The reset confirmation must be exactly 'demo-household'.")
    if reset and apply and confirm_household_wipe != DEMO_HOUSEHOLD_ID:
        raise DemoSeedError("Applying a reset requires explicit demo-household confirmation.")

    desired = manifest.documents()
    existing = _query_demo_household(container)
    _validate_existing_documents(existing)

    if reset:
        if apply:
            for document in existing:
                container.delete_item(
                    item=str(document["id"]),
                    partition_key=DEMO_HOUSEHOLD_ID,
                )
            _create_documents(container, desired)
            _verify_documents(container, desired)
        return DemoSeedResult(
            action="reset",
            applied=apply,
            records_planned=len(desired),
            records_created=len(desired) if apply else 0,
            documents_deleted=len(existing) if apply else 0,
        )

    if existing:
        if _documents_match(existing, desired):
            return DemoSeedResult(
                action="already_loaded",
                applied=apply,
                records_planned=0,
                records_created=0,
                documents_deleted=0,
            )
        raise DemoSeedError(
            "demo-household is not empty or does not exactly match this manifest; "
            "inspect it before using the explicit reset option."
        )

    if apply:
        _create_documents(container, desired)
        _verify_documents(container, desired)
    return DemoSeedResult(
        action="load",
        applied=apply,
        records_planned=len(desired),
        records_created=len(desired) if apply else 0,
        documents_deleted=0,
    )


def _query_demo_household(container: CosmosSeedContainer) -> list[dict[str, object]]:
    request_charges: list[str] = []

    def capture_response(headers: Mapping[str, str], _body: Mapping[str, object]) -> None:
        charge = headers.get("x-ms-request-charge")
        if charge is not None:
            request_charges.append(charge)

    documents = [
        dict(document)
        for document in container.query_items(
            query="SELECT * FROM c WHERE c.household_id = @household_id",
            parameters=[{"name": "@household_id", "value": DEMO_HOUSEHOLD_ID}],
            partition_key=DEMO_HOUSEHOLD_ID,
            response_hook=capture_response,
        )
    ]
    _LOGGER.info(
        "demo_seed_partition_read_completed documents=%d request_charges=%s",
        len(documents),
        ",".join(request_charges) or "unavailable",
    )
    return documents


def _validate_existing_documents(documents: Sequence[Mapping[str, object]]) -> None:
    seen_ids: set[str] = set()
    for document in documents:
        document_id = document.get("id")
        if document.get("household_id") != DEMO_HOUSEHOLD_ID:
            raise DemoSeedError("Cosmos returned a document outside demo-household.")
        if not isinstance(document_id, str) or not document_id:
            raise DemoSeedError("A demo-household document has no valid id.")
        if document_id in seen_ids:
            raise DemoSeedError("Cosmos returned a duplicate document id.")
        seen_ids.add(document_id)


def _without_cosmos_system_fields(document: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in document.items() if key not in _COSMOS_SYSTEM_FIELDS}


def _documents_match(
    existing: Sequence[Mapping[str, object]],
    desired: Sequence[Mapping[str, object]],
) -> bool:
    existing_by_id = {
        str(document["id"]): _without_cosmos_system_fields(document) for document in existing
    }
    desired_by_id = {
        str(document["id"]): _without_cosmos_system_fields(document) for document in desired
    }
    return existing_by_id == desired_by_id


def _create_documents(
    container: CosmosSeedContainer,
    documents: Sequence[Mapping[str, object]],
) -> None:
    for document in documents:
        container.create_item(body=document)


def _verify_documents(
    container: CosmosSeedContainer,
    desired: Sequence[Mapping[str, object]],
) -> None:
    actual = _query_demo_household(container)
    if not _documents_match(actual, desired):
        raise DemoSeedError("Demo seed write completed without the expected read-back state.")


def load_manifest(path: Path) -> DemoSeedManifest:
    return DemoSeedManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _cosmos_configuration(environment: Mapping[str, str]) -> dict[str, str]:
    names = {
        "endpoint": "NESTOCK_COSMOS_ENDPOINT",
        "database": "NESTOCK_COSMOS_DATABASE",
        "container": "NESTOCK_COSMOS_CONTAINER",
    }
    configured = {key: environment.get(name) for key, name in names.items()}
    if not any(value is not None for value in configured.values()):
        raise RuntimeError("Set the NESTOCK_COSMOS_* environment variables to use the Demo seeder.")
    if any(not value for value in configured.values()):
        missing = [names[key] for key, value in configured.items() if not value]
        raise RuntimeError("Incomplete Cosmos configuration; missing " + ", ".join(missing) + ".")
    return {key: str(value) for key, value in configured.items()}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load or reset the demo-household dataset.")
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="JSON file containing inventory, shopping, and transaction records.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply writes; without this flag, only inspect and plan.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete every document in demo-household before loading.",
    )
    parser.add_argument(
        "--confirm-household",
        choices=[DEMO_HOUSEHOLD_ID],
        help="Required with --apply --reset to confirm the destructive household reset.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.confirm_household and not (arguments.apply and arguments.reset):
        parser.error("--confirm-household is only valid with --apply --reset.")
    if arguments.apply and arguments.reset and arguments.confirm_household != DEMO_HOUSEHOLD_ID:
        parser.error("--apply --reset requires --confirm-household demo-household.")

    manifest = load_manifest(arguments.manifest)
    configuration = _cosmos_configuration(os.environ)
    container, close = create_azure_cosmos_seed_container(
        endpoint=configuration["endpoint"],
        database_name=configuration["database"],
        container_name=configuration["container"],
    )
    try:
        result = load_demo_seed(
            container,
            manifest,
            apply=arguments.apply,
            reset=arguments.reset,
            confirm_household_wipe=arguments.confirm_household,
        )
    finally:
        close()

    print(
        f"action={result.action} applied={result.applied} "
        f"household_id={DEMO_HOUSEHOLD_ID} records_planned={result.records_planned} "
        f"records_created={result.records_created} documents_deleted={result.documents_deleted}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
