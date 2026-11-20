import asyncio
import base64
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from app.agent.llm import VertexAISettings, build_vertex_client
from app.models.domain import MediaFailureReason
from app.models.vision import (
    DetectedItem,
    ImageComparisonResult,
    SceneType,
    VisionAnalysisResult,
)
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

    async def compare_images(
        self, household_id: str, image_ref_a: str, image_ref_b: str
    ) -> ImageComparisonResult: ...


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

    async def compare_images(
        self, household_id: str, image_ref_a: str, image_ref_b: str
    ) -> ImageComparisonResult:
        del household_id
        if not image_ref_a or not image_ref_a.strip():
            raise ValueError("image_ref_a must be a non-empty string reference.")
        if not image_ref_b or not image_ref_b.strip():
            raise ValueError("image_ref_b must be a non-empty string reference.")
        clean_a = image_ref_a.strip()
        clean_b = image_ref_b.strip()
        if clean_a == clean_b:
            return ImageComparisonResult(
                image_ref_a=clean_a,
                image_ref_b=clean_b,
                is_same_item=True,
                similarity_score=1.0,
                confidence=1.0,
                reasoning="Identical image reference provided (short-circuit).",
                visual_differences=[],
            )
        return ImageComparisonResult(
            image_ref_a=clean_a,
            image_ref_b=clean_b,
            is_same_item=False,
            similarity_score=0.15,
            confidence=0.95,
            reasoning="Mock visual comparison: items differ in visual appearance.",
            visual_differences=["Mock visual comparison: items differ in appearance."],
        )


def detect_image_mime(data: bytes, fallback: str = "image/jpeg") -> str:
    """Detect image MIME type from binary headers for Azure OpenAI Vision (JPEG, PNG, WebP)."""
    if not data or len(data) < 4:
        return fallback
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


class AzureOpenAIVisionService:
    """Azure OpenAI GPT-4o multimodal vision adapter (Issue #44 pure perception)."""

    def __init__(
        self,
        settings: VisionOpenAISettings | VertexAISettings,
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

    def _has_credentials(self) -> bool:
        if isinstance(self.settings, VertexAISettings):
            return bool(self.settings.project)
        return bool(self.settings.api_key and self.settings.endpoint)

    async def _request_openai_vision(
        self,
        media_bytes: bytes,
        scene_type: SceneType | None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        import httpx

        mime = content_type or detect_image_mime(media_bytes)
        b64 = base64.b64encode(media_bytes).decode("utf-8")
        hint = f"Scene hint: {scene_type.value if scene_type else 'auto'}"
        user_content = [
            {"type": "text", "text": hint},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
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
        self,
        household_id: str,
        clean_ref: str,
        media_bytes: bytes,
        scene_type: SceneType | None,
        content_type: str | None = None,
    ) -> VisionAnalysisResult:
        await self.media_storage.begin_processing(household_id, clean_ref)
        try:
            parsed = await self._request_openai_vision(
                media_bytes, scene_type, content_type=content_type
            )
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

        has_creds = self._has_credentials()
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

        content_type = None
        get_snapshot = getattr(self.media_storage, "get_media_snapshot", None)
        if callable(get_snapshot):
            try:
                snap = await get_snapshot(household_id, clean_ref)
                if snap is not None and getattr(snap, "record", None):
                    content_type = getattr(snap.record, "content_type", None)
            except Exception:
                pass

        result = await self._execute_inference(
            household_id, clean_ref, media_bytes, scene_type, content_type=content_type
        )
        self._results_cache[cache_key] = result
        return result

    async def compare_images(
        self, household_id: str, image_ref_a: str, image_ref_b: str
    ) -> ImageComparisonResult:
        from app.services.vision_comparison import compare_two_images

        return await compare_two_images(
            media_storage=self.media_storage,
            settings=self.settings,
            household_id=household_id,
            image_ref_a=image_ref_a,
            image_ref_b=image_ref_b,
            mock_fallback=self.mock_fallback,
            fallback_service=self._fallback_service,
        )


class VertexVisionService(AzureOpenAIVisionService):
    """Vertex AI Gemini vision adapter with the existing media lifecycle and DTOs."""

    def __init__(
        self,
        settings: VertexAISettings,
        inventory_service: object | None = None,
        media_storage: MediaStorageService | None = None,
        mock_fallback: bool = False,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            settings=settings,
            inventory_service=inventory_service,
            media_storage=media_storage,
            mock_fallback=mock_fallback,
        )
        self._vertex_client = client if client is not None else build_vertex_client(settings)

    async def _request_openai_vision(
        self,
        media_bytes: bytes,
        scene_type: SceneType | None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        from google.genai import types

        mime = content_type or detect_image_mime(media_bytes)
        prompt = f"{VISION_PROMPT}\nScene hint: {scene_type.value if scene_type else 'auto'}"
        response = await self._vertex_client.aio.models.generate_content(
            model=self.settings.model,
            contents=[types.Part.from_bytes(data=media_bytes, mime_type=mime), prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Vertex vision returned an empty response.")
        return json.loads(text)

    async def compare_images(
        self, household_id: str, image_ref_a: str, image_ref_b: str
    ) -> ImageComparisonResult:
        if not image_ref_a or not image_ref_a.strip():
            raise ValueError("image_ref_a must be a non-empty string reference.")
        if not image_ref_b or not image_ref_b.strip():
            raise ValueError("image_ref_b must be a non-empty string reference.")
        clean_a, clean_b = image_ref_a.strip(), image_ref_b.strip()
        if clean_a == clean_b:
            exists = await self.media_storage.verify_media_exists(household_id, clean_a)
            if not exists and not self.mock_fallback:
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

        bytes_a, bytes_b = await asyncio.gather(
            self.media_storage.get_media_bytes(household_id, clean_a),
            self.media_storage.get_media_bytes(household_id, clean_b),
        )
        if not bytes_a or not bytes_b:
            if self.mock_fallback:
                return await self._fallback_service.compare_images(household_id, clean_a, clean_b)
            raise ValueError("Media bytes not found for one or both image references.")

        from google.genai import types

        from app.services.vision_comparison import (
            COMPARE_VISION_PROMPT,
            _safe_bounded_float,
        )

        try:
            response = await self._vertex_client.aio.models.generate_content(
                model=self.settings.model,
                contents=[
                    COMPARE_VISION_PROMPT,
                    types.Part.from_bytes(data=bytes_a, mime_type=detect_image_mime(bytes_a)),
                    types.Part.from_bytes(data=bytes_b, mime_type=detect_image_mime(bytes_b)),
                ],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Vertex vision comparison returned an empty response.")
            parsed = json.loads(text)
            raw_same = parsed.get("is_same_item")
            if isinstance(raw_same, bool):
                same = raw_same
            elif isinstance(raw_same, str):
                same = raw_same.strip().lower() in ("true", "1", "yes")
            else:
                same = False
            score = _safe_bounded_float(parsed.get("similarity_score"), 0.9 if same else 0.1)
            confidence = _safe_bounded_float(parsed.get("confidence"), 0.8 if same else 0.5)
            differences = parsed.get("visual_differences")
            return ImageComparisonResult(
                image_ref_a=clean_a,
                image_ref_b=clean_b,
                is_same_item=same,
                similarity_score=score,
                confidence=confidence,
                reasoning=parsed.get("reasoning"),
                visual_differences=[
                    str(value).strip()
                    for value in differences
                    if isinstance(value, (str, int, float)) and str(value).strip()
                ]
                if isinstance(differences, list)
                else [],
            )
        except Exception as exc:
            logger.warning("Vertex vision comparison failed (%s)", exc)
            if self.mock_fallback:
                return await self._fallback_service.compare_images(household_id, clean_a, clean_b)
            raise RuntimeError(f"Vertex vision comparison failed: {exc}") from exc
