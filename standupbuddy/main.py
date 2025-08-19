import asyncio
import signal
import sys
import os
from aiohttp import web

from telegram import Update
from telegram.error import Conflict

from .app import build_app
from .config import BOT_TOKEN
from .db import init_db, db
from .jobs import reschedule_daily_job


async def health_check(request):
    """Simple health check endpoint for Railway"""
    return web.Response(text="OK", status=200)


async def start_web_server():
    """Start a simple web server for health checks"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', 8080)))
    await site.start()
    print(f"Health check server started on port {site.name}")
    return runner


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
        
        # Start health check server
        web_runner = await start_web_server()
        
        # Add error handling for Conflict error
        try:
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            print("StandupBuddy started.")
            try:
                await asyncio.Event().wait()
            except KeyboardInterrupt:
                print("Shutting down...")
            finally:
                await web_runner.cleanup()
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
        except Conflict as e:
            print(f"Bot conflict detected: {e}")
            print("This usually means another instance is running. Waiting 30 seconds before retry...")
            await asyncio.sleep(30)
            # Retry once
            try:
                await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                print("StandupBuddy started on retry.")
                await asyncio.Event().wait()
            except Exception as retry_error:
                print(f"Retry failed: {retry_error}")
                sys.exit(1)
            finally:
                await web_runner.cleanup()
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
        except Exception as e:
            print(f"Unexpected error: {e}")
            await web_runner.cleanup()
            sys.exit(1)

    # Handle graceful shutdown
    def signal_handler(signum, frame):
        print(f"Received signal {signum}, shutting down gracefully...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    asyncio.run(_run())


if __name__ == "__main__":
    main()


