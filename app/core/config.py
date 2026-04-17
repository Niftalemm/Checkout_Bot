from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Clawbot Checkout Assistant"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    database_url: str = "sqlite:///./clawbot.db"
    uploads_dir: str = "uploads"
    pricing_sheet_path: str = "data/pricing_sheet.json"
    schedule_path: str = "data/schedule.json"
    discord_bot_token: str = ""
    discord_guild_id: int | None = None
    discord_channel_id: int | None = None
    microsoft_form_url: str = ""
    auto_fill_on_complete: bool = True
    voice_note_require_text: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("discord_guild_id", "discord_channel_id", mode="before")
    @classmethod
    def _empty_string_to_none(cls, value):
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


settings = Settings()
