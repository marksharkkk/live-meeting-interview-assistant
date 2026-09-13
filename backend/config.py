from pathlib import Path
from typing import Any

from dotenv import set_key
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parent
ENV_FILE = BACKEND_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = ""
    openai_api_base: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    knowledge_base_dir: Path = BACKEND_DIR / "knowledge_base"
    host: str = "127.0.0.1"
    port: int = 8000
    voice_language: str = "auto"
    voice_sample_rate: int = 16000
    voice_input_device: int = -1


settings = Settings()
if not settings.knowledge_base_dir.is_absolute():
    settings.knowledge_base_dir = (BACKEND_DIR / settings.knowledge_base_dir).resolve()


_PERSISTED_KEYS = {
    "openai_api_key": "OPENAI_API_KEY",
    "openai_api_base": "OPENAI_API_BASE",
    "openai_model": "OPENAI_MODEL",
    "voice_language": "VOICE_LANGUAGE",
    "voice_input_device": "VOICE_INPUT_DEVICE",
}


def persist_settings(updates: dict[str, Any]) -> None:
    """Persist supported settings to the project-local .env file."""
    ENV_FILE.touch(exist_ok=True)
    for field, value in updates.items():
        env_key = _PERSISTED_KEYS.get(field)
        if env_key is None:
            continue
        text_value = str(value).strip()
        set_key(str(ENV_FILE), env_key, text_value, quote_mode="always")
        # Keep the in-memory settings type aligned with the Pydantic model.
        # This matters for numeric settings changed from the main window.
        if field == "voice_input_device":
            setattr(settings, field, int(text_value))
        else:
            setattr(settings, field, text_value)
