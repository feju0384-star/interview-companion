import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    answer_backend: Literal["api", "codex", "claude"] = "api"
    codex_executable: str = Field(default="", max_length=2000)
    codex_model: str = Field(default="", max_length=200)
    codex_effort: Literal["low", "medium", "high"] = "low"
    codex_fast: bool = False
    codex_timeout_seconds: int = Field(default=120, ge=15, le=300)
    claude_executable: str = Field(default="", max_length=2000)
    claude_model: str = Field(default="", max_length=200)
    claude_direct: bool = True
    claude_timeout_seconds: int = Field(default=120, ge=15, le=300)

    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = ""
    llm_api_key: str = Field(default="", max_length=2000, repr=False)
    asr_provider: Literal["compatible", "minimax"] = "compatible"
    asr_base_url: str = "https://api.siliconflow.cn/v1"
    asr_model: str = "FunAudioLLM/SenseVoiceSmall"
    asr_api_key: str = Field(default="", max_length=2000, repr=False)
    asr_language: str = Field(default="", max_length=10)
    role: str = Field(default="", max_length=200)
    background: str = Field(default="", max_length=12000)
    silence_seconds: float = Field(default=1.0, ge=0.4, le=3.0)
    asr_chunk_seconds: float = Field(default=4.0, ge=2.0, le=12.0)
    question_merge_seconds: float = Field(default=2.0, ge=0.0, le=5.0)
    energy_threshold: float = Field(default=0.008, ge=0.001, le=0.1)
    audio_gain: float = Field(default=1.0, ge=1.0, le=16.0)
    max_tokens: int = Field(default=900, ge=128, le=4000)
    auto_answer: bool = True

    @field_validator("llm_base_url", "asr_base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("请填写不含密钥、查询参数的 API Base URL")
        if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
            raise ValueError("云端 API 必须使用 https，本机服务可使用 http")
        if parsed.path.endswith(("/chat/completions", "/audio/transcriptions", "/speech_to_text")):
            raise ValueError("请填写 Base URL，无需加具体接口路径")
        return value.rstrip("/")

    @field_validator("llm_model", "asr_model")
    @classmethod
    def model_length(cls, value: str) -> str:
        if len(value) > 200:
            raise ValueError("模型名称过长")
        return value

    def public(self) -> dict:
        result = self.model_dump(exclude={"llm_api_key", "asr_api_key"})
        result.update(llm_key_saved=bool(self.llm_api_key), asr_key_saved=bool(self.asr_api_key))
        return result

    def check_ready(self, *, audio: bool = False) -> None:
        if self.answer_backend == "api" and (not self.llm_model or not self.llm_api_key):
            raise ValueError("请先在设置中填写回答模型名称和 API Key")
        if audio and (not self.asr_model or not self.asr_api_key):
            raise ValueError("请先在设置中填写语音识别模型和 API Key；它可以使用另一家服务")


class ConfigStore:
    def __init__(self, path: Path | None = None):
        folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".config"))) / "InterviewCompanion"
        self.path = path or folder / "settings.json"

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        return Settings.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(self.path)
