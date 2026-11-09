import base64
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from app.models.vision import (
    DetectedItem,
    SceneType,
    VisionAnalysisResult,
)
from app.services.normalizer import ItemNormalizer
from app.services.provisional_storage.interfaces import MediaStorageService

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class VisionOpenAISettings:
    """Dedicated Azure OpenAI configuration for Vision perception (A2)."""

    endpoint: str
    api_key: str
    deployment: str = "gpt-4o"

    @property
    def base_url(self) -> str:
        parts = urlsplit(self.endpoint if "://" in self.endpoint else f"https://{self.endpoint}")
        return f"{parts.scheme}://{parts.netloc}/openai/v1/"


def load_vision_azure_settings(
    environment: Mapping[str, str] | None = None,
) -> VisionOpenAISettings | None:
    """Load vision settings without process-wide dotenv side-effects.

    Decoupled model strategy:
    1. Endpoint and API Key prioritize AZURE_OPENAI_VISION_*; fallback to AZURE_OPENAI_*.
    2. Deployment prioritizes AZURE_OPENAI_VISION_DEPLOYMENT; fallback to AZURE_OPENAI_DEPLOYMENT
       or default to 'gpt-4o'.
    """
    env = os.environ if environment is None else environment

    endpoint = (
        env.get("AZURE_OPENAI_VISION_ENDPOINT", "").strip()
        or env.get("AZURE_OPENAI_ENDPOINT", "").strip()
    )
    api_key = (
        env.get("AZURE_OPENAI_VISION_API_KEY", "").strip()
        or env.get("AZURE_OPENAI_API_KEY", "").strip()
    )
    deployment = (
        env.get("AZURE_OPENAI_VISION_DEPLOYMENT", "").strip()
        or env.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
        or "gpt-4o"
    )

    if not endpoint or not api_key:
        return None

    return VisionOpenAISettings(endpoint=endpoint, api_key=api_key, deployment=deployment)


@runtime_checkable
class VisionService(Protocol):
    """Port for multimodal vision inference and item normalization."""

    async def analyze_image(
        self,
        household_id: str,
        image_ref: str,
        scene_type: SceneType | None = None,
    ) -> VisionAnalysisResult: ...


class MockVisionService:
    """Mock vision provider for local development and deterministic testing.

    Per Issue #44, A2 returns pure perception observations and never reads inventory.
    `inventory_service` is accepted only as an ignored compatibility parameter for
    existing composition callers.
    """

    def __init__(self, inventory_service: object | None = None) -> None:
        del inventory_service

    async def analyze_image(
        self,
        household_id: str,
        image_ref: str,
        scene_type: SceneType | None = None,
    ) -> VisionAnalysisResult:
        del household_id
        if not image_ref or not image_ref.strip():
            raise ValueError("image_ref must be a non-empty string reference.")

        effective_scene = scene_type or SceneType.SINGLE_ITEM

        # Baseline mock recognition scenario (pure observation, no inventory matching)
        raw_name = "統一陽光高纖無糖豆漿 450ml"
        canonical_name = "高纖無糖豆漿"

        item = DetectedItem(
            raw_detected_name=raw_name,
            canonical_name=canonical_name,
            quantity=1,
            unit="瓶",
            unit_confirmed=True,
            confidence=0.95,
        )

        return VisionAnalysisResult(
            image_ref=image_ref.strip(),
            scene_type=effective_scene,
            detected_items=[item],
            raw_text=raw_name,
        )


class AzureOpenAIVisionService:
    """Azure OpenAI GPT-4o multimodal vision adapter.

    Per Issue #44, outputs pure perception observations without reading inventory.
    """

    def __init__(
        self,
        settings: VisionOpenAISettings,
        inventory_service: object | None = None,
        media_storage: MediaStorageService | None = None,
        mock_fallback: bool = False,
    ) -> None:
        if media_storage is None and isinstance(inventory_service, MediaStorageService):
            media_storage = inventory_service
        if media_storage is None:
            raise ValueError("media_storage is required for AzureOpenAIVisionService.")
        self.settings = settings
        self.media_storage = media_storage
        self.mock_fallback = mock_fallback
        self._fallback_service = MockVisionService()
        self._normalizer = ItemNormalizer()

    async def analyze_image(
        self,
        household_id: str,
        image_ref: str,
        scene_type: SceneType | None = None,
    ) -> VisionAnalysisResult:
        if not image_ref or not image_ref.strip():
            raise ValueError("image_ref must be a non-empty string reference.")

        # Offline / unconfigured protection
        if not self.settings.api_key or not self.settings.endpoint:
            if self.mock_fallback:
                return await self._fallback_service.analyze_image(
                    household_id, image_ref, scene_type
                )
            raise ValueError("Azure OpenAI Vision credentials not configured.")

        # Retrieve media bytes
        media_bytes = await self.media_storage.get_media_bytes(household_id, image_ref.strip())
        if not media_bytes:
            if self.mock_fallback:
                return await self._fallback_service.analyze_image(
                    household_id, image_ref, scene_type
                )
            raise ValueError(f"Media bytes not found for image_ref: {image_ref}")

        try:
            import httpx

            base64_img = base64.b64encode(media_bytes).decode("utf-8")
            url = f"{self.settings.base_url}chat/completions"
            headers = {
                "api-key": self.settings.api_key,
                "Content-Type": "application/json",
            }
            system_prompt = (
                "You are an expert household item and Taiwanese retail product recognition system. "
                "Extract recognized household items, consumer electronics, groceries, pantry goods, daily necessities, or receipt line items. "
                'Output JSON with schema: {"scene_type": "single_item"|"pantry_scene"|"receipt", '
                '"raw_text": str, "items": [{"raw_name": str, "quantity": int, '
                '"unit": str, "confidence": float}]}. '
                "For confidence, provide a float between 0.0 and 1.0 representing recognition "
                "certainty (e.g., 0.95 for clearly visible items/text, "
                "0.45 for blurry or occluded items). "
                "For units, use standard Taiwanese units (瓶, 罐, 包, 盒, 袋, 入, 箱, 個, 支, 台) or null. "
                "Remove marketing noise like 特價, 買一送一."
            )
            payload = {
                "model": self.settings.deployment,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Scene hint: {scene_type.value if scene_type else 'auto'}",
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"},
                            },
                        ],
                    },
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code != 200:
                    logger.warning(
                        "Azure OpenAI Vision returned %s: %s", resp.status_code, resp.text
                    )
                    if self.mock_fallback:
                        return await self._fallback_service.analyze_image(
                            household_id, image_ref, scene_type
                        )
                    resp.raise_for_status()

                body = resp.json()
                content = body["choices"][0]["message"]["content"]
                parsed = json.loads(content)

            # Build pure perception observations without reading inventory (Issue #44)
            detected_items: list[DetectedItem] = []
            for raw_item in parsed.get("items", []):
                raw_name = raw_item.get("raw_name", "").strip()
                if not raw_name:
                    # [DEFENSIVE_GUARD] Skip empty ghost items from LLM to maintain
                    # DetectedItem min_length=1 contract.
                    continue

                norm = self._normalizer.normalize(raw_name)
                canonical_name = norm.canonical_name
                keywords = norm.feature_keywords
                raw_qty = raw_item.get("quantity")
                try:
                    qty = (
                        max(1, int(round(float(raw_qty)))) if raw_qty is not None else norm.quantity
                    )
                except (ValueError, TypeError):
                    qty = norm.quantity
                unit = raw_item.get("unit") or norm.unit
                unit_confirmed = bool(unit)

                raw_conf = raw_item.get("confidence")
                try:
                    confidence = float(raw_conf) if raw_conf is not None else 0.5
                    confidence = max(0.0, min(1.0, round(confidence, 2)))
                except (ValueError, TypeError):
                    confidence = 0.5

                detected_items.append(
                    DetectedItem(
                        raw_detected_name=raw_name,
                        canonical_name=canonical_name,
                        feature_keywords=keywords,
                        quantity=qty,
                        unit=unit,
                        unit_confirmed=unit_confirmed,
                        confidence=confidence,
                    )
                )

            effective_scene = scene_type
            if not effective_scene:
                scene_str = parsed.get("scene_type", "")
                try:
                    effective_scene = SceneType(scene_str)
                except ValueError:
                    effective_scene = SceneType.SINGLE_ITEM

            return VisionAnalysisResult(
                image_ref=image_ref.strip(),
                scene_type=effective_scene,
                detected_items=detected_items,
                raw_text=parsed.get("raw_text"),
            )
        except Exception as exc:
            logger.warning("Azure OpenAI Vision execution failed (%s)", exc)
            if self.mock_fallback:
                return await self._fallback_service.analyze_image(
                    household_id, image_ref, scene_type
                )
            if isinstance(exc, (ValueError, RuntimeError)):
                raise
            raise RuntimeError(f"Azure OpenAI Vision inference failed: {exc}") from exc
