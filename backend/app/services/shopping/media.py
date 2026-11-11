"""Provisional C1 attachment handoff over B1's media mapping port (Issue #98).

B1's media mapping port is available in main. Only an explicitly configured
adapter may plan attachments. This module never writes a mapping separately from business
writes and never infers that a removed reference was the last active attachment.
"""

from dataclasses import dataclass
from typing import Protocol

from app.services.approvals import TargetChanged
from app.services.cosmos_write import ReplaceDocument
from app.services.media_mapping import build_media_mapping_retention_update
from app.services.shopping.preview import ShoppingPreviewError


@dataclass(frozen=True)
class MediaAttachment:
    operation_id: str
    input_id: str
    image_ref: str
    mapping_etag: str

    def __post_init__(self):
        for value in (self.operation_id, self.input_id, self.image_ref, self.mapping_etag):
            if not isinstance(value, str) or not value.strip():
                raise ShoppingPreviewError("Attachment identity, reference and ETag are required.")


class MediaRetentionPlanner(Protocol):
    async def prepare_attachment(
        self, *, household_id: str, attachment: MediaAttachment
    ) -> ReplaceDocument: ...


class B1MediaRetentionPlanner:
    """Use PR #97's completed lookup and ETag retention helper, without I/O writes."""

    def __init__(self, mapping_storage):
        self._storage = mapping_storage

    async def prepare_attachment(
        self, *, household_id: str, attachment: MediaAttachment
    ) -> ReplaceDocument:
        snapshot = await self._storage.find_completed_by_image_ref(
            household_id=household_id, image_ref=attachment.image_ref
        )
        if snapshot is None:
            raise TargetChanged(f"input:{attachment.input_id}")
        if (
            snapshot.record.household_id != household_id
            or snapshot.record.input_id != attachment.input_id
            or snapshot.record.image_ref != attachment.image_ref
            or snapshot.record.status != "completed"
            or snapshot.etag != attachment.mapping_etag
        ):
            raise TargetChanged(f"input:{attachment.input_id}")
        # No finite mapping TTL while its business attachment remains active.
        return build_media_mapping_retention_update(snapshot, ttl_seconds=None)
