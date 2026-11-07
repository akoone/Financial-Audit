"""Entry point: runs the Telegram bot, the report scheduler and the web server (Mini App) together.

Messages arrive by long polling on an always-on server, or by webhook on hosts that sleep
without traffic (Render). See BOT_MODE in .env.example."""
from __future__ import annotations

import asyncio
import logging
import os
import time

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from . import db, scheduler
from .bot import UserMiddleware, router
from .config import Settings, get_settings
from .webapp import create_app

log = logging.getLogger("app.main")

COMMANDS = [
    ("app", "Open the app"),
    ("report", "Report with chart and AI advice"),
    ("balance", "All account balances"),
    ("networth", "Assets, debts and net worth"),
    ("history", "Recent transactions"),
    ("undo", "Delete the last entry"),
    ("statement", "Month-end statement"),
    ("backup", "Full backup file"),
    ("accounts", "Manage accounts"),
    ("reports", "Automatic report times"),
    ("connect", "Automatic bank alerts"),
    ("export", "Download CSV"),
    ("help", "How to use"),
]
SHORT_DESCRIPTION = "Track spending, income, accounts and net worth. Receipts, bank alerts, reports and an app with charts."
DESCRIPTION = (
    "Track all your money in one place: spending, income, accounts, debts and net worth.\n\n"
    "• Just type it: \"Spent 50k on lunch\" or \"+3m salary\"\n"
    "• Send a receipt photo or a payment screenshot\n"
    "• Forward bank SMS alerts\n"
    "• Daily, weekly and monthly reports with charts\n"
    "• Open the app for the dashboard, history and statements\n\n"
    "Your records are private to you."
)


async def setup_profile(bot: Bot, settings: Settings):
    """Commands, the 'Open app' menu button and the bot's description. Problems here are not fatal."""
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
        if settings.https_url:
            # Only set when this copy has a public HTTPS address, so a test copy never removes
            # the button of the real one.
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="Open app", web_app=WebAppInfo(url=f"{settings.https_url}/app")))
        # Fill the descriptions only if they are empty, so text set in @BotFather is kept.
        if not (await bot.get_my_short_description()).short_description:
            await bot.set_my_short_description(SHORT_DESCRIPTION)
        if not (await bot.get_my_description()).description:
            await bot.set_my_description(DESCRIPTION)
    except Exception as e:
        log.warning("Could not update the bot profile: %s", e)


async def poll(bot: Bot, dp: Dispatcher, settings: Settings):
    """Long polling. If another copy of the bot receives messages by webhook, wait instead of
    stealing them, unless that webhook is failing or BOT_MODE=polling is set."""
    while True:
        info = await bot.get_webhook_info()
        if not info.url:
            break
        failing = info.last_error_date and time.time() - info.last_error_date.timestamp() < 600
        if settings.bot_mode.strip().lower() == "polling" or failing:
            reason = "BOT_MODE=polling" if not failing else f"it is failing: {info.last_error_message}"
            log.warning("Removing the webhook %s (%s) and switching to polling.", info.url, reason)
            await bot.delete_webhook(drop_pending_updates=False)
            break
        log.warning("Another copy of this bot receives messages at %s. Not taking over; checking again in "
                    "60 s. (Stop that copy, or set BOT_MODE=polling here to take over.)", info.url)
        await asyncio.sleep(60)
    log.info("Polling mode: asking Telegram for new messages")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")
    if settings.render and db.describe() == "SQLite file":
        raise SystemExit("DATABASE_URL is not set. Render wipes the disk at every restart, so the bot needs an "
                         "external database: paste your Neon connection string into DATABASE_URL (see DEPLOY.md).")
    db.init_engine()
    await db.create_all()
    log.info("Database: %s", db.describe())

    # Optional outbound proxy for Telegram (BOT_PROXY, or the standard HTTPS_PROXY variable)
    proxy = os.environ.get("BOT_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(router)
    await setup_profile(bot, settings)
    await scheduler.start(bot)

    app = create_app(bot, dp if settings.use_webhook else None)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.listen_port, log_level="warning",
                                           proxy_headers=True, forwarded_allow_ips="*"))
    if settings.https_url:
        log.info("Mini App: %s/app", settings.https_url)
    else:
        log.info("No public HTTPS address (WEBAPP_URL), so the Mini App button is off")
    if settings.use_webhook:
        url = f"{settings.https_url}/telegram/webhook"
        await bot.set_webhook(url, secret_token=settings.webhook_token, drop_pending_updates=False,
                              allowed_updates=dp.resolve_used_update_types())
        log.info("Webhook mode: Telegram sends messages to %s", url)
        try:
            await server.serve()
        finally:
            await bot.session.close()
    else:
        await asyncio.gather(poll(bot, dp, settings), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
