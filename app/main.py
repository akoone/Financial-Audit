"""Entry point: runs the Telegram bot (long polling), report scheduler and web server together."""
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from . import db, scheduler
from .bot import UserMiddleware, router
from .config import get_settings
from .webapp import create_app

COMMANDS = [
    ("report", "Report with chart and AI advice"),
    ("balance", "All account balances"),
    ("networth", "Assets, debts and net worth"),
    ("dashboard", "Interactive charts"),
    ("history", "Recent transactions"),
    ("undo", "Delete the last entry"),
    ("accounts", "Manage accounts"),
    ("reports", "Automatic report times"),
    ("connect", "Automatic bank alerts"),
    ("export", "Download CSV"),
    ("help", "How to use"),
]


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")
    db.init_engine()
    await db.create_all()

    # Optional outbound proxy for Telegram (BOT_PROXY, or the standard HTTPS_PROXY variable)
    proxy = os.environ.get("BOT_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(router)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    await scheduler.start(bot)

    server = uvicorn.Server(uvicorn.Config(create_app(bot), host="0.0.0.0", port=settings.web_port,
                                           log_level="warning"))
    await asyncio.gather(dp.start_polling(bot), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
