"""Database connection options and automatic upgrades of older databases."""
import sqlalchemy as sa

from app import db
from app.config import Settings

NEON = ("postgresql://neondb_owner:secret@ep-shy-cake-123-pooler.c-8.us-east-2.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require")


def test_neon_pooler_options():
    url, kw = db.engine_options(NEON)
    assert url.startswith("postgresql+asyncpg://") and "ssl=require" in url and "channel_binding" not in url
    assert "prepared_statement_cache_size=0" in url
    assert kw["connect_args"]["statement_cache_size"] == 0
    name = kw["connect_args"]["prepared_statement_name_func"]
    assert name() != name()  # unique statement names for the pooler
    assert kw["pool_pre_ping"] is True
    # a direct (non-pooler) connection keeps normal prepared statements
    url2, kw2 = db.engine_options(NEON.replace("-pooler", ""))
    assert "connect_args" not in kw2 and "prepared_statement_cache_size" not in url2
    assert db.uses_pgbouncer("postgresql://u:p@db.x.supabase.co:6543/postgres")
    assert db.engine_options("sqlite+aiosqlite:///:memory:")[1] == {}
    # the engine accepts these options (no connection is made here)
    eng = sa.ext.asyncio.create_async_engine(url, **kw)
    assert eng.dialect.name == "postgresql"


def test_describe_hides_password():
    assert db.describe(NEON) == "PostgreSQL (Neon)"
    assert db.describe("sqlite+aiosqlite:///./data/finance.db") == "SQLite file"
    assert "secret" not in db.describe(NEON)


async def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    # A database made by an older version: the accounts table lacks some columns
    eng = sa.create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id BIGINT, name VARCHAR(100), "
                          "base_currency VARCHAR(3), timezone VARCHAR(64), default_account_id INTEGER, "
                          "ingest_token VARCHAR(64), created_at DATETIME)"))
        c.execute(sa.text("CREATE TABLE accounts (id INTEGER PRIMARY KEY, user_id INTEGER, name VARCHAR(60), "
                          "type VARCHAR(20), currency VARCHAR(3), opening_balance NUMERIC(18,4))"))
        c.execute(sa.text("INSERT INTO users (id, telegram_id, name, base_currency, timezone) VALUES (1, 9, 'A', 'USD', 'UTC')"))
        c.execute(sa.text("INSERT INTO accounts VALUES (1, 1, 'Cash', 'cash', 'USD', 10)"))
    eng.dispose()

    db.init_engine(f"sqlite+aiosqlite:///{path}")
    await db.create_all()
    async with db.session() as s:
        from app.models import Account, User
        acc = await s.get(Account, 1)
        user = await s.get(User, 1)
        assert acc.archived is False and acc.include_in_net_worth is True  # added with their defaults
        assert user.language == "en"
        assert float(acc.opening_balance) == 10  # existing data untouched
    await db.create_all()  # running again changes nothing


def test_modes():
    s = Settings(bot_token="x", _env_file=None)
    assert not s.use_webhook and s.listen_port == 8080 and s.https_url == ""
    s = Settings(bot_token="x", render_external_url="https://money.onrender.com", port=10000, _env_file=None)
    assert s.use_webhook and s.https_url == "https://money.onrender.com" and s.listen_port == 10000
    s = Settings(bot_token="x", render_external_url="https://money.onrender.com", bot_mode="polling", _env_file=None)
    assert not s.use_webhook
    s = Settings(bot_token="x", webapp_url="https://bot.example.com/", _env_file=None)
    assert s.https_url == "https://bot.example.com" and not s.use_webhook  # own server: polling by default
    s = Settings(bot_token="x", webapp_url="https://bot.example.com", bot_mode="webhook", _env_file=None)
    assert s.use_webhook
    s = Settings(bot_token="x", webapp_url="http://insecure", bot_mode="webhook", _env_file=None)
    assert not s.use_webhook  # Telegram requires HTTPS
    assert Settings(bot_token="a", _env_file=None).webhook_token != Settings(bot_token="b", _env_file=None).webhook_token
