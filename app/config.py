import hashlib
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

    # Public HTTPS address of the web server (Mini App, bank alerts, webhook)
    webapp_url: str = ""
    web_port: int = 8080
    # How the bot receives messages: auto | polling | webhook.
    # auto = webhook on Render (it sleeps without traffic), polling everywhere else.
    bot_mode: str = "auto"
    webhook_secret: str = ""
    # Set automatically by hosts such as Render
    port: int = 0
    render_external_url: str = ""
    render: bool = False

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.allowed_user_ids.replace(" ", "").split(",") if x}

    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def public_url(self) -> str:
        return (self.webapp_url or self.render_external_url).strip().rstrip("/")

    @property
    def https_url(self) -> str:
        """Public URL if it is HTTPS (Telegram Mini Apps and webhooks require HTTPS), else ''."""
        return self.public_url if self.public_url.startswith("https://") else ""

    @property
    def listen_port(self) -> int:
        return self.port or self.web_port

    @property
    def use_webhook(self) -> bool:
        mode = self.bot_mode.strip().lower()
        if mode == "polling" or not self.https_url:
            return False
        return mode == "webhook" or bool(self.render_external_url)

    @property
    def webhook_token(self) -> str:
        """Secret Telegram sends with every webhook call, so nobody else can post fake updates."""
        if self.webhook_secret:
            return self.webhook_secret
        return hashlib.sha256(f"webhook:{self.bot_token}".encode()).hexdigest()[:48]


@lru_cache
def get_settings() -> Settings:
    return Settings()
