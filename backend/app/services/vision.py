import base64
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from dotenv import load_dotenv

from app.models.vision import (
    CandidateMatch,
    DetectedItem,
    MatchStatus,
    SceneType,
    VisionAnalysisResult,
)
from app.services.interfaces import InventoryService
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
    """Load vision settings.

    Decoupled model strategy:
    1. Endpoint and API Key prioritize AZURE_OPENAI_VISION_*; fallback to AZURE_OPENAI_*.
    2. Deployment prioritizes AZURE_OPENAI_VISION_DEPLOYMENT; fallback to AZURE_OPENAI_DEPLOYMENT
       or default to 'gpt-4o'.
    """
    if environment is None:
        load_dotenv(BACKEND_DIR / ".env", override=False)
        environment = os.environ

    endpoint = (
        environment.get("AZURE_OPENAI_VISION_ENDPOINT", "").strip()
        or environment.get("AZURE_OPENAI_ENDPOINT", "").strip()
    )
    api_key = (
        environment.get("AZURE_OPENAI_VISION_API_KEY", "").strip()
        or environment.get("AZURE_OPENAI_API_KEY", "").strip()
    )
    deployment = (
        environment.get("AZURE_OPENAI_VISION_DEPLOYMENT", "").strip()
        or environment.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
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
    """Mock vision provider for local development and deterministic testing."""

    def __init__(self, inventory_service: InventoryService) -> None:
        self.inventory_service = inventory_service

    async def analyze_image(
        self,
        household_id: str,
        image_ref: str,
        scene_type: SceneType | None = None,
    ) -> VisionAnalysisResult:
        if not image_ref or not image_ref.strip():
            raise ValueError("image_ref must be a non-empty string reference.")

        effective_scene = scene_type or SceneType.SINGLE_ITEM
        existing_items = await self.inventory_service.list_items(household_id)

        # Baseline mock recognition scenario
        raw_name = "統一陽光高纖無糖豆漿 450ml"
        canonical_name = "高纖無糖豆漿"
        keywords = ["豆漿", "無糖", "高纖"]

        # Match against inventory
        matched_id: str | None = None
        match_status = MatchStatus.NEW_ITEM
        candidate_matches: list[CandidateMatch] = []

        # 1. Exact or alias match
        for item in existing_items:
            if item.name == canonical_name:
                matched_id = item.item_id
                match_status = MatchStatus.EXACT_MATCH
                break
            if item.aliases and canonical_name in item.aliases:
                matched_id = item.item_id
                match_status = MatchStatus.ALIAS_MATCH
                break

        # 2. If not exact/alias, check for fuzzy candidates based on feature_keywords overlap
        if matched_id is None:
            fuzzy_hits: list[CandidateMatch] = []
            for item in existing_items:
                matching_kw = [
                    kw
                    for kw in keywords
                    if kw in item.name or (item.aliases and any(kw in a for a in item.aliases))
                ]
                if matching_kw:
                    score = min(1.0, 0.5 + 0.15 * len(matching_kw))
                    fuzzy_hits.append(
                        CandidateMatch(item_id=item.item_id, name=item.name, score=score)
                    )

            if fuzzy_hits:
                match_status = MatchStatus.FUZZY_CANDIDATE
                candidate_matches = fuzzy_hits
                if len(candidate_matches) == 1:
                    matched_id = candidate_matches[0].item_id

        item = DetectedItem(
            raw_detected_name=raw_name,
            canonical_name=canonical_name,
            feature_keywords=keywords,
            quantity=1.0,
            unit="瓶",
            unit_confirmed=True,
            confidence=0.95,
            matched_inventory_id=matched_id,
            candidate_matches=candidate_matches,
            match_status=match_status,
        )

        return VisionAnalysisResult(
            image_ref=image_ref.strip(),
            scene_type=effective_scene,
            detected_items=[item],
            raw_text=raw_name,
        )


class AzureOpenAIVisionService:
    """Azure OpenAI GPT-4o multimodal vision adapter with graceful fallback."""

    def __init__(
        self,
        settings: VisionOpenAISettings,
        inventory_service: InventoryService,
        media_storage: MediaStorageService,
        mock_fallback: bool = True,
    ) -> None:
        self.settings = settings
        self.inventory_service = inventory_service
        self.media_storage = media_storage
        self.mock_fallback = mock_fallback
        self._fallback_service = MockVisionService(inventory_service)
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
                "You are an expert Taiwanese FMCG and retail product recognition system. "
                "Extract recognized grocery/pantry items or receipt line items. "
                'Output JSON with schema: {"scene_type": "single_item"|"pantry_scene"|"receipt", '
                '"raw_text": str, "items": [{"raw_name": str, "quantity": float, "unit": str}]}. '
                "For units, use standard Taiwanese units (瓶, 罐, 包, 盒, 袋, 入, 箱, 個) or null. "
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

            # Match extracted items with inventory
            existing_items = await self.inventory_service.list_items(household_id)
            detected_items: list[DetectedItem] = []
            for raw_item in parsed.get("items", []):
                raw_name = raw_item.get("raw_name", "").strip()
                if raw_name:
                    norm = self._normalizer.normalize(raw_name)
                    canonical_name = norm.canonical_name
                    keywords = norm.feature_keywords
                    qty = float(raw_item.get("quantity") or norm.quantity or 1.0)
                    unit = raw_item.get("unit") or norm.unit
                else:
                    canonical_name = "未知名稱"
                    keywords = []
                    qty = float(raw_item.get("quantity") or 1.0)
                    unit = raw_item.get("unit")
                unit_confirmed = bool(unit)

                matched_id = None
                match_status = MatchStatus.NEW_ITEM
                candidate_matches = []

                for item in existing_items:
                    has_alias = item.aliases and canonical_name in item.aliases
                    if item.name == canonical_name or has_alias:
                        matched_id = item.item_id
                        match_status = MatchStatus.EXACT_MATCH
                        break

                if matched_id is None:
                    fuzzy_hits = []
                    for item in existing_items:
                        matching_kw = [
                            kw
                            for kw in keywords
                            if kw in item.name
                            or (item.aliases and any(kw in a for a in item.aliases))
                        ]
                        if matching_kw:
                            score = min(1.0, 0.5 + 0.15 * len(matching_kw))
                            fuzzy_hits.append(
                                CandidateMatch(item_id=item.item_id, name=item.name, score=score)
                            )
                    if fuzzy_hits:
                        match_status = MatchStatus.FUZZY_CANDIDATE
                        candidate_matches = fuzzy_hits
                        if len(candidate_matches) == 1:
                            matched_id = candidate_matches[0].item_id

                detected_items.append(
                    DetectedItem(
                        raw_detected_name=raw_name,
                        canonical_name=canonical_name,
                        feature_keywords=keywords,
                        quantity=qty,
                        unit=unit,
                        unit_confirmed=unit_confirmed,
                        confidence=0.9,
                        matched_inventory_id=matched_id,
                        candidate_matches=candidate_matches,
                        match_status=match_status,
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
            logger.warning("Azure OpenAI Vision execution failed (%s), fallback to mock.", exc)
            if self.mock_fallback:
                return await self._fallback_service.analyze_image(
                    household_id, image_ref, scene_type
                )
            raise
