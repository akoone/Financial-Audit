import logging
import os
import uuid
from urllib.parse import urlsplit

from sqlalchemy import inspect, literal, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base

log = logging.getLogger(__name__)
_engine = None
_sessionmaker = None


def normalize_url(url: str) -> str:
    """Accept connection strings as hosted providers give them (Neon, Supabase, Render...).

    'postgres://' / 'postgresql://' -> async driver; 'sslmode=require' -> 'ssl=require';
    drops libpq-only options (channel_binding) that the async driver rejects."""
    url = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix):]
    if url.startswith("postgresql+asyncpg://") and "?" in url:
        base, query = url.split("?", 1)
        params = []
        for p in query.split("&"):
            key, _, val = p.partition("=")
            if key == "sslmode":
                params.append(f"ssl={val}")
            elif key not in ("channel_binding", "options", "pgbouncer"):
                params.append(p)
        url = base + ("?" + "&".join(params) if params else "")
    return url


def uses_pgbouncer(raw_url: str) -> bool:
    """Connection poolers in transaction mode (Neon '-pooler' hosts, Supabase port 6543, '?pgbouncer=true')."""
    try:
        parts = urlsplit(raw_url.strip())
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return False
    return "-pooler." in host or port == 6543 or "pgbouncer=true" in raw_url.lower()


def engine_options(raw_url: str) -> tuple[str, dict]:
    """(url, create_async_engine kwargs) for this database."""
    url = normalize_url(raw_url)
    if url.startswith("sqlite"):
        return url, {}
    kwargs = {
        # Hosted databases close idle connections (Neon sleeps after 5 minutes without queries):
        # test each connection before use and replace old ones, so a sleeping database just wakes up.
        "pool_pre_ping": True, "pool_recycle": 280, "pool_size": 5, "max_overflow": 5,
    }
    if uses_pgbouncer(raw_url):
        # The pooler may run each transaction on a different server connection, so prepared
        # statements must have unique names and must not be cached.
        sep = "&" if "?" in url else "?"
        url += f"{sep}prepared_statement_cache_size=0"
        kwargs["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        }
    return url, kwargs


def describe(raw_url: str | None = None) -> str:
    """Human-readable database description without the password, e.g. 'PostgreSQL (Neon)'."""
    url = normalize_url(raw_url or get_settings().database_url or "sqlite")
    if url.startswith("sqlite"):
        return "SQLite file"
    host = urlsplit(url).hostname or ""
    for domain, name in (("neon.tech", "Neon"), ("supabase", "Supabase"), ("render.com", "Render")):
        if domain in host:
            return f"PostgreSQL ({name})"
    return "PostgreSQL"


def init_engine(url: str | None = None):
    global _engine, _sessionmaker
    raw = url or get_settings().database_url or "sqlite+aiosqlite:///./data/finance.db"
    url, kwargs = engine_options(raw)
    if url.startswith("sqlite") and ":///./" in url:
        os.makedirs(os.path.dirname(url.split(":///", 1)[1]) or ".", exist_ok=True)
    _engine = create_async_engine(url, **kwargs)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _add_missing_columns(sync_conn) -> list[str]:
    """Bring an existing database up to date with new versions of the code.

    create_all() makes missing tables but never changes existing ones, so columns added in a
    later version are added here. Only additive changes: nothing is ever dropped or rewritten."""
    insp = inspect(sync_conn)
    added = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect=sync_conn.dialect)}'
            default = col.default.arg if col.default is not None and col.default.is_scalar else None
            if default is not None:
                rendered = literal(default).compile(dialect=sync_conn.dialect, compile_kwargs={"literal_binds": True})
                ddl += f" DEFAULT {rendered}"
            sync_conn.execute(text(ddl))
            added.append(f"{table.name}.{col.name}")
    return added


async def create_all():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        added = await conn.run_sync(_add_missing_columns)
    if added:
        log.info("Database upgraded, new columns: %s", ", ".join(added))


def session() -> AsyncSession:
    if _sessionmaker is None:
        init_engine()
    return _sessionmaker()
