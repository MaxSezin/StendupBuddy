import asyncio

from telegram import Update

from .app import build_app
from .config import BOT_TOKEN
from .db import init_db, db
from .jobs import reschedule_daily_job


async def restore_jobs(app):
    conn = db()
    teams = conn.execute("SELECT id FROM teams WHERE reminder_time IS NOT NULL").fetchall()
    for r in teams:
        await reschedule_daily_job(app, r["id"])


def main():
    if not BOT_TOKEN:
        raise SystemExit("Установите BOT_TOKEN в окружении.")
    init_db()
    app = build_app()

    async def _run():
        await restore_jobs(app)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("StandupBuddy started.")
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    asyncio.run(_run())


if __name__ == "__main__":
    main()


