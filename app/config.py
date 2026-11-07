from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = ""
    allowed_user_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///./data/finance.db"

    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-5-5"
    ai_daily_limit: int = 40
    ocr_langs: str = "eng+rus+uzb+uzb_cyrl"

    default_currency: str = "USD"
    default_timezone: str = "UTC"

    webapp_url: str = ""
    web_port: int = 8080

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_user_ids.replace(" ", "").split(",") if x}

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
