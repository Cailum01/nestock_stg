from enum import StrEnum

from pydantic import Field

from app.models.base import ContractModel


class AllowedAudioContentType(StrEnum):
    AUDIO_M4A = "audio/m4a"
    AUDIO_MP4 = "audio/mp4"
    AUDIO_X_M4A = "audio/x-m4a"
    AUDIO_AAC = "audio/aac"
    AUDIO_WAV = "audio/wav"
    AUDIO_MPEG = "audio/mpeg"
    AUDIO_WEBM = "audio/webm"


class SpeechTranscribeResponse(ContractModel):
    """Transcription result envelope from speech-to-text service."""

    transcript: str = Field(description="轉錄文字內容")
    detected_language: str = Field(default="zh-TW", description="AI 實際偵測或指定之語言代碼")
    duration_seconds: float | None = Field(default=None, ge=0.0, description="音訊時長秒數")
