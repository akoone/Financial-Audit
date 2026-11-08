"""Sends the daily / weekly / monthly reports at each user's chosen local time."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from . import db
from .models import ReportSchedule, User

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()
_bot: Bot | None = None

CRON = {"daily": {}, "weekly": {"day_of_week": "sun"}, "monthly": {"day": 1}}


async def _run(user_id: int, kind: str):
    from datetime import timedelta

    from . import services as sv
    from .bot import send_backup, send_report, statement_text
    async with db.session() as s:
        user = await s.get(User, user_id)
        if user is None:
            return
        try:
            if kind == "snapshot":
                await sv.take_snapshot(s, user)
            else:
                await send_report(_bot, s, user, kind)
                if kind == "monthly":
                    # Month closing: statement for the month that just ended + a full backup file
                    end = sv.local_today(user).replace(day=1) - timedelta(days=1)
                    st = await sv.statement(s, user, end.replace(day=1), end)
                    await _bot.send_message(user.telegram_id, statement_text(st))
                    await send_backup(_bot, s, user, caption="🗄 Monthly backup. Telegram keeps this file in "
                                                             "the chat, so you always have a copy of your data.")
            await s.commit()
        except Exception:
            log.exception("%s for user %s failed", kind, user_id)


def schedule_user(user: User, rows: list[ReportSchedule] | None = None):
    """(Re)create this user's report jobs. Called at startup and when settings change."""
    import asyncio

    async def _load_and_schedule():
        async with db.session() as s:
            sched = rows or list((await s.execute(select(ReportSchedule).where(
                ReportSchedule.user_id == user.id))).scalars())
        for kind in CRON:
            job_id = f"{user.id}:{kind}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
        # Nightly record of all balances and net worth (always on)
        scheduler.add_job(_run, CronTrigger(hour=23, minute=55, timezone=user.timezone),
                          args=[user.id, "snapshot"], id=f"{user.id}:snapshot", replace_existing=True,
                          misfire_grace_time=6 * 3600)
        for r in sched:
            if not r.enabled:
                continue
            hh, mm = map(int, r.send_time.split(":"))
            scheduler.add_job(_run, CronTrigger(hour=hh, minute=mm, timezone=user.timezone, **CRON[r.kind]),
                              args=[user.id, r.kind], id=f"{user.id}:{r.kind}", replace_existing=True,
                              misfire_grace_time=3600)

    if not scheduler.running:
        return
    asyncio.get_running_loop().create_task(_load_and_schedule())


async def start(bot: Bot):
    global _bot
    _bot = bot
    scheduler.start()
    async with db.session() as s:
        users = list((await s.execute(select(User))).scalars())
        for u in users:
            rows = list((await s.execute(select(ReportSchedule).where(ReportSchedule.user_id == u.id))).scalars())
            schedule_user(u, rows)
    log.info("Scheduled reports for %d users", len(users))
    asyncio.get_running_loop().create_task(_ensure_today_snapshots())
    from .config import get_settings
    st = get_settings()
    if st.keep_awake and st.render_external_url:
        # Render's free plan sleeps after 15 minutes without outside traffic, which also stops
        # scheduled reports. Calling our own public address counts as traffic. One always-on
        # service uses at most 744 of the 750 free hours a month.
        from apscheduler.triggers.interval import IntervalTrigger
        scheduler.add_job(_keep_awake, IntervalTrigger(minutes=10), id="keep_awake", replace_existing=True)
        log.info("Keep-awake ping every 10 minutes")


async def _keep_awake():
    import httpx
    from .config import get_settings
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.get(get_settings().render_external_url.rstrip("/") + "/health")
    except Exception as e:  # never let a failed ping disturb the bot
        log.warning("keep-awake ping failed: %s", e)


async def _ensure_today_snapshots():
    """Make sure today's balance record exists even if the server was asleep or restarting at 23:55.
    (The nightly job replaces it with the end-of-day version.)"""
    from . import services as sv
    from .models import Snapshot
    async with db.session() as s:
        for u in list((await s.execute(select(User))).scalars()):
            have = (await s.execute(select(Snapshot.id).where(
                Snapshot.user_id == u.id, Snapshot.day == sv.local_today(u)))).first()
            if have:
                continue
            try:
                await sv.take_snapshot(s, u)
                await s.commit()
            except Exception:
                await s.rollback()
                log.exception("startup snapshot for user %s failed", u.id)
