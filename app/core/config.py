from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Clawbot Checkout Assistant"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    database_url: str = "sqlite:///./clawbot.db"
    uploads_dir: str = "uploads"
    pricing_sheet_path: str = "data/pricing_sheet.json"
    form_mapping_path: str = "data/form_mapping.json"
    schedule_path: str = "data/schedule.json"
    discord_bot_token: str = ""
    discord_guild_id: int | None = None
    discord_channel_id: int | None = None
    microsoft_form_url: str = ""
    playwright_storage_state_path: str = "runtime/playwright/storage_state.json"
    playwright_headless: bool = False
    playwright_debug: bool = False
    playwright_debug_dir: str = "runtime/playwright/debug"
    playwright_manual_review_timeout_seconds: int = 900
    playwright_auto_submit_headless: bool = True
    default_has_bathroom: bool = True
    auto_fill_on_complete: bool = False
    standalone_reminders_enabled: bool = False
    voice_note_require_text: bool = True
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "openai/gpt-oss-20b"
    groq_transcription_model: str = "whisper-large-v3-turbo"
    groq_timeout_seconds: int = 20

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("discord_guild_id", "discord_channel_id", mode="before")
    @classmethod
    def _empty_string_to_none(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


settings = Settings()
