"""[A2_PERCEPTION] Multimodal Vision Comparison Module (Issue #176 & Issue #44).

Provides objective visual comparison between two opaque image references without
reading or depending on inventory databases (pure perception boundary).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING, Any

from app.models.vision import ImageComparisonResult

if TYPE_CHECKING:
    from app.services.provisional_storage.interfaces import MediaStorageService
    from app.services.vision import VisionOpenAISettings

logger = logging.getLogger(__name__)

COMPARE_VISION_PROMPT = (
    "You are a multimodal vision comparison expert for household inventory management. "
    "Compare two images (Image A and Image B) of physical household products or groceries. "
    "Determine if they depict the exact same product/item (same brand, product line, and variant). "
    "Output JSON strictly with keys: "
    '{"is_same_item": bool, "similarity_score": float, "confidence": float, '
    '"reasoning": str, "visual_differences": [str]}. '
    "similarity_score: 0.0 to 1.0 (1.0 = identical product, 0.0 = completely different item). "
    "confidence: 0.0 to 1.0. "
    "visual_differences: list of observable differences (e.g. flavor, volume, packaging) "
    "or empty list if identical."
)


def _safe_bounded_float(
    val: Any, default: float, min_val: float = 0.0, max_val: float = 1.0
) -> float:
    try:
        f = float(val) if val is not None else default
        return max(min_val, min(max_val, round(f, 2)))
    except (ValueError, TypeError):
        return default


def _detect_mime(data: bytes, fallback: str = "image/jpeg") -> str:
    if not data or len(data) < 4:
        return fallback
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


async def _execute_multimodal_compare(
    settings: VisionOpenAISettings,
    bytes_a: bytes,
    bytes_b: bytes,
) -> dict[str, Any]:
    import httpx

    mime_a = _detect_mime(bytes_a)
    mime_b = _detect_mime(bytes_b)
    b64_a = base64.b64encode(bytes_a).decode("utf-8")
    b64_b = base64.b64encode(bytes_b).decode("utf-8")

    user_content = [
        {"type": "text", "text": "Compare these two household item images:"},
        {"type": "image_url", "image_url": {"url": f"data:{mime_a};base64,{b64_a}"}},
        {"type": "image_url", "image_url": {"url": f"data:{mime_b};base64,{b64_b}"}},
    ]
    messages = [
        {"role": "system", "content": COMPARE_VISION_PROMPT},
        {"role": "user", "content": user_content},
    ]
    payload: dict[str, Any] = {"model": settings.deployment, "messages": messages}
    payload["response_format"] = {"type": "json_object"}
    if settings.temperature is not None:
        payload["temperature"] = settings.temperature
    headers = {"api-key": settings.api_key, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{settings.base_url}chat/completions", headers=headers, json=payload
        )
        if resp.status_code != 200:
            logger.warning(
                "Azure OpenAI Vision Comparison returned %s: %s", resp.status_code, resp.text
            )
            raise RuntimeError(
                f"Azure OpenAI Vision Comparison failed ({resp.status_code}): {resp.text}"
            )
        return json.loads(resp.json()["choices"][0]["message"]["content"])  # type: ignore[no-any-return]


async def compare_two_images(
    media_storage: MediaStorageService,
    settings: VisionOpenAISettings,
    household_id: str,
    image_ref_a: str,
    image_ref_b: str,
    mock_fallback: bool = False,
    fallback_service: Any = None,
) -> ImageComparisonResult:
    """Compare two images under household_id for perceptual equivalence.

    Invariants:
    - Never queries InventoryItem or Cosmos DB (Issue #44).
    - Short-circuits with score=1.0 if image_ref_a == image_ref_b.
    - Preserves deterministic mock fallback when credentials are absent or in tests.
    """
    if not image_ref_a or not image_ref_a.strip():
        raise ValueError("image_ref_a must be a non-empty string reference.")
    if not image_ref_b or not image_ref_b.strip():
        raise ValueError("image_ref_b must be a non-empty string reference.")

    clean_a = image_ref_a.strip()
    clean_b = image_ref_b.strip()

    # Short-circuit optimization for identical references
    if clean_a == clean_b:
        exists = await media_storage.verify_media_exists(household_id, clean_a)
        if not exists and not mock_fallback:
            raise ValueError(f"Media not found for image_ref: {clean_a}")
        return ImageComparisonResult(
            image_ref_a=clean_a,
            image_ref_b=clean_b,
            is_same_item=True,
            similarity_score=1.0,
            confidence=1.0,
            reasoning="Identical image reference provided (short-circuit).",
            visual_differences=[],
        )

    has_creds = bool(settings.api_key and settings.endpoint)
    if not has_creds:
        if mock_fallback and fallback_service is not None:
            return await fallback_service.compare_images(household_id, clean_a, clean_b)  # type: ignore[no-any-return]
        raise ValueError("Azure OpenAI Vision credentials not configured.")

    bytes_a, bytes_b = await asyncio.gather(
        media_storage.get_media_bytes(household_id, clean_a),
        media_storage.get_media_bytes(household_id, clean_b),
    )
    if not bytes_a:
        if mock_fallback and fallback_service is not None:
            return await fallback_service.compare_images(household_id, clean_a, clean_b)  # type: ignore[no-any-return]
        raise ValueError(f"Media bytes not found for image_ref_a: {clean_a}")
    if not bytes_b:
        if mock_fallback and fallback_service is not None:
            return await fallback_service.compare_images(household_id, clean_a, clean_b)  # type: ignore[no-any-return]
        raise ValueError(f"Media bytes not found for image_ref_b: {clean_b}")

    try:
        parsed = await _execute_multimodal_compare(settings, bytes_a, bytes_b)
        is_same = bool(parsed.get("is_same_item", False))
        sim = _safe_bounded_float(parsed.get("similarity_score"), 0.9 if is_same else 0.1)
        conf = _safe_bounded_float(parsed.get("confidence"), 0.8)
        diffs = [
            str(d) for d in parsed.get("visual_differences", []) if isinstance(d, (str, int, float))
        ]
        return ImageComparisonResult(
            image_ref_a=clean_a,
            image_ref_b=clean_b,
            is_same_item=is_same,
            similarity_score=sim,
            confidence=conf,
            reasoning=parsed.get("reasoning"),
            visual_differences=diffs,
        )
    except Exception as exc:
        logger.warning("Azure OpenAI Vision Comparison failed (%s)", exc)
        if mock_fallback and fallback_service is not None:
            return await fallback_service.compare_images(household_id, clean_a, clean_b)  # type: ignore[no-any-return]
        raise RuntimeError(f"Azure OpenAI Vision Comparison inference failed: {exc}") from exc
