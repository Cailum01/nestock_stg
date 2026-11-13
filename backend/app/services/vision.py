import base64
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from app.models.domain import MediaFailureReason
from app.models.vision import DetectedItem, SceneType, VisionAnalysisResult
from app.services.normalizer import ItemNormalizer
from app.services.provisional_storage.interfaces import MediaStorageService

logger = logging.getLogger(__name__)

VISION_PROMPT = (
    "Extract recognized household items, consumer electronics, groceries, pantry goods, "
    "daily necessities, or receipt line items. Output JSON: "
    '{"scene_type": "single_item"|"pantry_scene"|"receipt", "raw_text": str, "items": '
    '[{"raw_name": str, "quantity": int, "unit": str, "confidence": float}]}. '
    "Units: standard Taiwanese units (瓶, 罐, 包, 盒, 袋, 入, 箱, 個, 支, 台) or null."
)


def _safe_float(val: Any, default: float) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class VisionOpenAISettings:
    """Dedicated Azure OpenAI configuration for Vision perception (A2)."""

    endpoint: str
    api_key: str
    deployment: str = "gpt-4o"
    temperature: float | None = None

    @property
    def base_url(self) -> str:
        parts = urlsplit(self.endpoint if "://" in self.endpoint else f"https://{self.endpoint}")
        return f"{parts.scheme}://{parts.netloc}/openai/v1/"


def load_vision_azure_settings(
    environment: Mapping[str, str] | None = None,
) -> VisionOpenAISettings | None:
    env = os.environ if environment is None else environment
    ep = (env.get("AZURE_OPENAI_VISION_ENDPOINT") or env.get("AZURE_OPENAI_ENDPOINT") or "").strip()
    key = (env.get("AZURE_OPENAI_VISION_API_KEY") or env.get("AZURE_OPENAI_API_KEY") or "").strip()
    dep = (
        env.get("AZURE_OPENAI_VISION_DEPLOYMENT") or env.get("AZURE_OPENAI_DEPLOYMENT") or ""
    ).strip() or "gpt-4o"
    if not ep or not key:
        return None
    raw_t = env.get("AZURE_OPENAI_VISION_TEMPERATURE", "").strip()
    parsed_t = _safe_float(raw_t, float("nan")) if raw_t else float("nan")
    temp = None if parsed_t != parsed_t else parsed_t
    return VisionOpenAISettings(endpoint=ep, api_key=key, deployment=dep, temperature=temp)


@runtime_checkable
class VisionService(Protocol):
    """Port for multimodal vision inference and item normalization."""

    async def analyze_image(
        self, household_id: str, image_ref: str, scene_type: SceneType | None = None
    ) -> VisionAnalysisResult: ...


class MockVisionService:
    """Mock vision provider for local development and deterministic testing (pure observation)."""

    def __init__(self, inventory_service: object | None = None) -> None:
        del inventory_service

    async def analyze_image(
        self, household_id: str, image_ref: str, scene_type: SceneType | None = None
    ) -> VisionAnalysisResult:
        del household_id
        if not image_ref or not image_ref.strip():
            raise ValueError("image_ref must be a non-empty string reference.")
        raw_name = "統一陽光高纖無糖豆漿 450ml"
        kw = {"raw_detected_name": raw_name, "canonical_name": "高纖無糖豆漿", "quantity": 1}
        item = DetectedItem(unit="瓶", unit_confirmed=True, confidence=0.95, **kw)
        scene = scene_type or SceneType.SINGLE_ITEM
        return VisionAnalysisResult(
            image_ref=image_ref.strip(), scene_type=scene, detected_items=[item], raw_text=raw_name
        )


class AzureOpenAIVisionService:
    """Azure OpenAI GPT-4o multimodal vision adapter (Issue #44 pure perception)."""

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
        self._results_cache: dict[tuple[str, str], VisionAnalysisResult] = {}

    async def _request_openai_vision(
        self, media_bytes: bytes, scene_type: SceneType | None
    ) -> dict[str, Any]:
        import httpx

        b64 = base64.b64encode(media_bytes).decode("utf-8")
        hint = f"Scene hint: {scene_type.value if scene_type else 'auto'}"
        user_content = [
            {"type": "text", "text": hint},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
        messages = [
            {"role": "system", "content": VISION_PROMPT},
            {"role": "user", "content": user_content},
        ]
        payload: dict[str, Any] = {"model": self.settings.deployment, "messages": messages}
        payload["response_format"] = {"type": "json_object"}
        if self.settings.temperature is not None:
            payload["temperature"] = self.settings.temperature
        headers = {"api-key": self.settings.api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.settings.base_url}chat/completions", headers=headers, json=payload
            )
            if resp.status_code != 200:
                logger.warning("Azure OpenAI Vision returned %s: %s", resp.status_code, resp.text)
                raise RuntimeError(
                    f"Azure OpenAI Vision inference failed ({resp.status_code}): {resp.text}"
                )
            return json.loads(resp.json()["choices"][0]["message"]["content"])  # type: ignore[no-any-return]

    def _parse_detected_item(self, raw_item: dict[str, Any]) -> DetectedItem | None:
        raw_name = raw_item.get("raw_name", "").strip()
        if not raw_name:
            # [DEFENSIVE_GUARD] Skip empty ghost items from LLM (min_length=1 contract).
            return None
        norm = self._normalizer.normalize(raw_name)
        qty = max(1, round(_safe_float(raw_item.get("quantity"), float(norm.quantity))))
        conf = max(0.0, min(1.0, round(_safe_float(raw_item.get("confidence"), 0.5), 2)))
        unit = raw_item.get("unit") or norm.unit
        kw = {"raw_detected_name": raw_name, "canonical_name": norm.canonical_name}
        kw2 = {"quantity": qty, "unit": unit, "unit_confirmed": bool(unit), "confidence": conf}
        return DetectedItem(feature_keywords=norm.feature_keywords, **kw, **kw2)

    def _build_analysis_result(
        self, clean_ref: str, parsed: dict[str, Any], scene_type: SceneType | None
    ) -> VisionAnalysisResult:
        # Build pure perception observations without reading inventory (Issue #44)
        items = [i for r in parsed.get("items", []) if (i := self._parse_detected_item(r))]
        raw_scene = parsed.get("scene_type", "")
        scene = scene_type or (
            SceneType(raw_scene)
            if raw_scene in SceneType._value2member_map_
            else SceneType.SINGLE_ITEM
        )
        kw = {"image_ref": clean_ref, "scene_type": scene, "detected_items": items}
        return VisionAnalysisResult(raw_text=parsed.get("raw_text"), **kw)

    async def _record_inference_failure(self, household_id: str, clean_ref: str) -> None:
        try:
            await self.media_storage.fail_processing(
                household_id, clean_ref, MediaFailureReason.PROCESSING_FAILED
            )
        except KeyError as fail_exc:
            logger.debug("Optional fail_processing skipped for unmapped media: %s", fail_exc)

    async def _try_load_persisted_artifact(
        self, household_id: str, clean_ref: str
    ) -> VisionAnalysisResult | None:
        get_artifact = getattr(self.media_storage, "get_derived_artifact", None)
        if callable(get_artifact):
            artifact_bytes = await get_artifact(household_id, clean_ref)
            if isinstance(artifact_bytes, (bytes, str)) and len(artifact_bytes) > 0:
                return VisionAnalysisResult.model_validate_json(artifact_bytes)
        return None

    async def _execute_inference(
        self, household_id: str, clean_ref: str, media_bytes: bytes, scene_type: SceneType | None
    ) -> VisionAnalysisResult:
        await self.media_storage.begin_processing(household_id, clean_ref)
        try:
            parsed = await self._request_openai_vision(media_bytes, scene_type)
            result = self._build_analysis_result(clean_ref, parsed, scene_type)
        except Exception as exc:
            logger.warning("Azure OpenAI Vision execution failed (%s)", exc)
            if self.mock_fallback:
                result = await self._fallback_service.analyze_image(
                    household_id, clean_ref, scene_type
                )
            else:
                await self._record_inference_failure(household_id, clean_ref)
                if isinstance(exc, (ValueError, RuntimeError)):
                    raise
                raise RuntimeError(f"Azure OpenAI Vision inference failed: {exc}") from exc
        await self.media_storage.complete_processing(
            household_id, clean_ref, result.model_dump_json()
        )
        return result

    async def analyze_image(
        self, household_id: str, image_ref: str, scene_type: SceneType | None = None
    ) -> VisionAnalysisResult:
        if not image_ref or not image_ref.strip():
            raise ValueError("image_ref must be a non-empty string reference.")
        clean_ref = image_ref.strip()
        cache_key = (household_id, clean_ref)
        if cache_key in self._results_cache:
            return self._results_cache[cache_key]

        persisted = await self._try_load_persisted_artifact(household_id, clean_ref)
        if persisted is not None:
            self._results_cache[cache_key] = persisted
            return persisted

        has_creds = bool(self.settings.api_key and self.settings.endpoint)
        media_bytes = (
            await self.media_storage.get_media_bytes(household_id, clean_ref) if has_creds else None
        )
        if not media_bytes:
            if self.mock_fallback:
                return await self._fallback_service.analyze_image(
                    household_id, clean_ref, scene_type
                )
            if not has_creds:
                raise ValueError("Azure OpenAI Vision credentials not configured.")
            raise ValueError(f"Media bytes not found for image_ref: {image_ref}")

        result = await self._execute_inference(household_id, clean_ref, media_bytes, scene_type)
        self._results_cache[cache_key] = result
        return result
