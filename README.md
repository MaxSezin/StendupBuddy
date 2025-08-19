## StandupBuddy

Telegram bot that automates daily stand-ups for small teams. Built on python-telegram-bot v21.6 and pytz, with SQLite for storage.

### Features
- Create or join teams via invite code
- Managers (team creators) can configure schedule: time (HH:MM), timezone (UTC±N), and days (every day, weekdays, weekends, or custom)
- At the scheduled time, all members get a prompt to submit one reply with yesterday/today/blockers
- Automatic reminder to those who didn’t answer; daily summary sent to everyone and managers
- Manual “Run now” for managers
- Group menu: view participants, remove members, leave group, view/edit/delete schedule, view team info (name, ID, invite code, timezone, next run)
- UX safeguards: Back/Cancel buttons on all screens
- Jobs are restored on restart

### Requirements
- Python 3.10+
- `python-telegram-bot==21.6`
- `pytz`

Install dependencies:
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

### Configuration
Set your bot token from @BotFather:
```bash
export BOT_TOKEN=123456:ABC-YourTokenHere
```

### Run
Either entry works (both use the same code):
```bash
python stendup_bot.py
# or
python -m standupbuddy.main
```

On first run, SQLite schema is created automatically in `dailybot.db` in the project root.

### Usage in Telegram
- Send `/start` to the bot to open the main menu
- Create a team (you become manager) or join with an invite code
- As manager, set schedule:
  - Enter time as `HH:MM` (e.g., `09:30`)
  - Choose timezone as `UTC±N` (buttons) or enter like `UTC+3`
  - Pick days: presets or custom selection (Mon–Sun)
- Optional: tap “Run now” to trigger a stand-up immediately
- Members reply to the bot’s message with a single update message
- After the collection window, a summary is sent to all members and (also) to managers

Commands:
- `/start` — open menu
- `/help` — short help
- `/health` — shows DB connectivity and number of scheduled jobs

### Data Model (SQLite)
- `users (tg_id, name)`
- `teams (id, name, invite_code, tz, reminder_time, reminder_days, managers_json)`
- `team_members (team_id, tg_id)`
- `standups (id, team_id, date_iso, started_utc, remind_job_key, summary_job_key)`
- `updates (id, standup_id, tg_id, text, created_utc, answered)`

### Project structure
```
standupbuddy/
  __init__.py
  config.py        # constants: BOT_TOKEN, DB_PATH, timings
  db.py            # SQLite connection + schema init
  utils.py         # timezones, parsing, next-run computation
  keyboards.py     # InlineKeyboard builders
  states.py        # conversation state constants
  jobs.py          # scheduling: start/remind/summary
  handlers.py      # bot handlers and flows
  app.py           # Application/Conversation wiring
  main.py          # startup (init DB, restore jobs, polling)
stendup_bot.py     # thin entrypoint calling standupbuddy.main
```

### Deployment notes

#### Railway
1. Connect your GitHub repo to Railway
2. Set environment variable `BOT_TOKEN` in Railway dashboard
3. Deploy - Railway will use the `Procfile` to run `python -m standupbuddy.main`

#### Local/Server
Keep the process running via your preferred supervisor (systemd, pm2, Docker). Example systemd unit:
```ini
[Unit]
Description=StandupBuddy
After=network.target

[Service]
WorkingDirectory=/opt/standupbuddy
Environment=BOT_TOKEN=123456:ABC-YourTokenHere
ExecStart=/opt/standupbuddy/.venv/bin/python -m standupbuddy.main
Restart=always

[Install]
WantedBy=multi-user.target
```

### Troubleshooting
- Import errors for `telegram.*`: ensure dependencies are installed from `requirements.txt`
- No messages received: verify `BOT_TOKEN` and that you are chatting with the correct bot
- Jobs not firing: check `/health` output; on restart, jobs are restored from DB for teams with schedules

#### Railway-specific issues:
- **"Conflict: terminated by other getUpdates request"**: This happens when multiple bot instances run simultaneously during deployment. The bot now waits 30 seconds and retries automatically.
- **Deployment fails**: Check Railway logs for Python version compatibility or missing dependencies
- **Bot not responding**: Verify `BOT_TOKEN` is set correctly in Railway environment variables
- **Health check fails**: The bot runs a web server on the `PORT` environment variable for health checks

