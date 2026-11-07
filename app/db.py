import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings
from .models import Base

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
            elif key not in ("channel_binding", "options"):
                params.append(p)
        url = base + ("?" + "&".join(params) if params else "")
    return url


def init_engine(url: str | None = None):
    global _engine, _sessionmaker
    url = normalize_url(url or get_settings().database_url or "sqlite+aiosqlite:///./data/finance.db")
    if url.startswith("sqlite") and ":///./" in url:
        os.makedirs(os.path.dirname(url.split(":///", 1)[1]) or ".", exist_ok=True)
    kwargs = {} if url.startswith("sqlite") else {"pool_pre_ping": True, "pool_recycle": 280}
    _engine = create_async_engine(url, future=True, **kwargs)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def create_all():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def session() -> AsyncSession:
    if _sessionmaker is None:
        init_engine()
    return _sessionmaker()
