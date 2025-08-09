# main.py
import asyncio
import json
import os
import random
import sqlite3
import string
from datetime import datetime, timedelta, time, timezone

import pytz
from telegram import (
    Update,
    ForceReply,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

DB_PATH = "dailybot.db"
BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- Настройки окна дэйлика ---
REMIND_AFTER_MIN = 10      # минут до повторного напоминания
SUMMARY_AFTER_MIN = 30     # минут до итоговой сводки

# ---------- БАЗА ДАННЫХ ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS users (
            tg_id      INTEGER PRIMARY KEY,
            name       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS teams (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            invite_code     TEXT UNIQUE NOT NULL,
            tz              TEXT NOT NULL DEFAULT 'UTC',
            reminder_time   TEXT,                -- 'HH:MM'
            managers_json   TEXT NOT NULL        -- JSON-массив tg_id
        );

        CREATE TABLE IF NOT EXISTS team_members (
            team_id INTEGER NOT NULL,
            tg_id   INTEGER NOT NULL,
            UNIQUE(team_id, tg_id)
        );

        -- Один дэйлик на команду в день
        CREATE TABLE IF NOT EXISTS standups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id         INTEGER NOT NULL,
            date_iso        TEXT NOT NULL,       -- YYYY-MM-DD (дата в TZ команды)
            started_utc     TEXT NOT NULL,       -- ISOUTC
            remind_job_key  TEXT,                -- ключи джобов для возможной отмены
            summary_job_key TEXT
        );

        CREATE TABLE IF NOT EXISTS updates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            standup_id  INTEGER NOT NULL,
            tg_id       INTEGER NOT NULL,
            text        TEXT,
            created_utc TEXT,
            answered    INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_team_on_members ON team_members(team_id);
        CREATE INDEX IF NOT EXISTS idx_updates_standup ON updates(standup_id);
        """
    )
    conn.commit()
    conn.close()

def gen_invite_code(n=8):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))

def get_user_name(u: Update):
    user = u.effective_user
    if not user:
        return "Unknown"
    full = " ".join(x for x in [user.first_name, user.last_name] if x)
    return full or (user.username or str(user.id))

# ---------- HELPERS ----------
async def reply(u: Update, text: str, **kwargs):
    if u.effective_message:
        await u.effective_message.reply_text(text, **kwargs)

def now_utc():
    return datetime.now(timezone.utc)

def today_in_tz(tz_name: str):
    tz = pytz.timezone(tz_name)
    return datetime.now(tz).date()

def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))

def time_to_str(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"

# ---------- КОМАНДЫ ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = db()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (tg_id, name) VALUES (?, ?)",
            (update.effective_user.id, get_user_name(update)),
        )
    await reply(
        update,
        "Привет! Я бот для дэйликов.\n\n"
        "Команды:\n"
        "/create_team <имя> — создать команду (вы — менеджер)\n"
        "/invite <team_id> — получить инвайт‑код\n"
        "/join <код> — вступить в команду\n"
        "/set_time <team_id> <HH:MM> <TZ> — время дэйлика, напр. 10:00 Europe/Moscow\n"
        "/my_teams — список команд\n"
        "/standup_now <team_id> — запустить дэйлик прямо сейчас\n"
    )

async def cmd_create_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await reply(update, "Использование: /create_team <имя_команды>")
    name = " ".join(ctx.args).strip()
    code = gen_invite_code()
    manager_id = update.effective_user.id

    conn = db()
    with conn:
        cur = conn.execute(
            "INSERT INTO teams (name, invite_code, tz, reminder_time, managers_json) VALUES (?, ?, 'UTC', NULL, ?)",
            (name, code, json.dumps([manager_id])),
        )
        team_id = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO team_members (team_id, tg_id) VALUES (?, ?)",
            (team_id, manager_id),
        )

    await reply(
        update,
        f"Команда создана!\n"
        f"ID: {team_id}\n"
        f"Имя: {name}\n"
        f"Инвайт‑код: {code}\n"
        f"Часовой пояс: UTC (пока)\n"
        f"Назначьте время дэйлика: /set_time {team_id} 10:00 Europe/Moscow\n"
        f"Ссылка для входа: /join {code}",
    )

async def cmd_invite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await reply(update, "Использование: /invite <team_id>")
    team_id = int(ctx.args[0])

    conn = db()
    row = conn.execute("SELECT invite_code, name FROM teams WHERE id=?", (team_id,)).fetchone()
    if not row:
        return await reply(update, "Команда не найдена.")
    await reply(
        update,
        f"Команда: {row['name']} (ID {team_id})\nИнвайт‑код: {row['invite_code']}\n"
        f"Отправь участникам: /join {row['invite_code']}",
    )

async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await reply(update, "Использование: /join <код>")
    code = ctx.args[0].strip().upper()
    user_id = update.effective_user.id

    conn = db()
    team = conn.execute("SELECT id, name FROM teams WHERE invite_code=?", (code,)).fetchone()
    if not team:
        return await reply(update, "Неверный код.")
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO team_members (team_id, tg_id) VALUES (?, ?)",
            (team["id"], user_id),
        )
    await reply(update, f"Ок! Вы в команде «{team['name']}» (ID {team['id']}).")

async def cmd_my_teams(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    conn = db()
    rows = conn.execute(
        """
        SELECT t.id, t.name, t.tz, t.reminder_time, t.managers_json
        FROM teams t
        JOIN team_members m ON m.team_id=t.id
        WHERE m.tg_id=?
        ORDER BY t.id
        """,
        (uid,),
    ).fetchall()
    if not rows:
        return await reply(update, "Вы пока не в командах.")
    lines = []
    for r in rows:
        managers = json.loads(r["managers_json"])
        is_mgr = "да" if uid in managers else "нет"
        lines.append(
            f"ID {r['id']}: {r['name']} | TZ {r['tz']} | time {r['reminder_time'] or '—'} | менеджер: {is_mgr}"
        )
    await reply(update, "Ваши команды:\n" + "\n".join(lines))

async def cmd_set_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # /set_time <team_id> <HH:MM> <TZ>
    if len(ctx.args) < 3:
        return await reply(update, "Использование: /set_time <team_id> <HH:MM> <TZ>\nНапр.: /set_time 1 10:00 Europe/Moscow")

    team_id = int(ctx.args[0])
    hhmm = ctx.args[1]
    tz_name = " ".join(ctx.args[2:])

    # валидируем
    try:
        t = parse_hhmm(hhmm)
    except Exception:
        return await reply(update, "Время должно быть в формате HH:MM, напр. 09:30")
    try:
        pytz.timezone(tz_name)
    except Exception:
        return await reply(update, f"Неизвестный TZ: {tz_name}")

    uid = update.effective_user.id
    conn = db()
    team = conn.execute("SELECT managers_json, name FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        return await reply(update, "Команда не найдена.")
    managers = json.loads(team["managers_json"])
    if uid not in managers:
        return await reply(update, "Только менеджер может менять время.")

    with conn:
        conn.execute(
            "UPDATE teams SET reminder_time=?, tz=? WHERE id=?",
            (time_to_str(t), tz_name, team_id),
        )

    # Пересоздаём ежедневную задачу
    await reschedule_daily_job(ctx.application, team_id)

    await reply(
        update,
        f"Ок! Для команды «{team['name']}» время дэйлика: {hhmm} ({tz_name}).\n"
        f"Буду напоминать ежедневно.",
    )

async def cmd_standup_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        return await reply(update, "Использование: /standup_now <team_id>")
    team_id = int(ctx.args[0])

    uid = update.effective_user.id
    conn = db()
    team = conn.execute("SELECT name, tz, managers_json FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        return await reply(update, "Команда не найдена.")
    if uid not in json.loads(team["managers_json"]):
        return await reply(update, "Только менеджер может запускать дэйлик.")

    await start_standup(ctx.application, team_id, manual=True)
    await reply(update, f"Дэйлик для «{team['name']}» стартовал.")

# ---------- ЛОГИКА ДЭЙЛИКА ----------
async def reschedule_daily_job(app: Application, team_id: int):
    """Снимаем старые и ставим ежедневную джобу по времени команды."""
    # Удаляем прежнюю (по имени 'daily_<team_id>')
    job_name = f"daily_{team_id}"
    current = app.job_queue.get_jobs_by_name(job_name)
    for j in current:
        j.schedule_removal()

    conn = db()
    team = conn.execute("SELECT reminder_time, tz FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team or not team["reminder_time"]:
        return  # времени нет — нечего ставить

    hhmm = parse_hhmm(team["reminder_time"])
    tz = pytz.timezone(team["tz"])

    # Следующий запуск в TZ команды
    # PTB 21.x поддерживает .run_daily с tzinfo
    app.job_queue.run_daily(
        callback=daily_job_callback,
        time=hhmm,
        days=(0, 1, 2, 3, 4, 5, 6),
        name=job_name,
        data={"team_id": team_id},
        tzinfo=tz,
    )

async def daily_job_callback(ctx: ContextTypes.DEFAULT_TYPE):
    team_id = ctx.job.data["team_id"]
    await start_standup(ctx.application, team_id)

async def start_standup(app: Application, team_id: int, manual: bool = False):
    conn = db()
    team = conn.execute(
        "SELECT id, name, tz FROM teams WHERE id=?",
        (team_id,),
    ).fetchone()
    if not team:
        return

    tz_name = team["tz"]
    today = today_in_tz(tz_name).isoformat()

    # Проверим, не создан ли уже дэйлик сегодня
    existed = conn.execute(
        "SELECT id FROM standups WHERE team_id=? AND date_iso=?",
        (team_id, today),
    ).fetchone()
    if existed and not manual:
        return  # уже есть; при ручном запуске даём создать ещё один (на ту же дату)

    # Список участников
    members = conn.execute(
        "SELECT tg_id FROM team_members WHERE team_id=?",
        (team_id,),
    ).fetchall()
    members = [r["tg_id"] for r in members]
    if not members:
        return

    # Создаём standup
    with conn:
        cur = conn.execute(
            "INSERT INTO standups (team_id, date_iso, started_utc) VALUES (?, ?, ?)",
            (team_id, today, now_utc().isoformat()),
        )
        standup_id = cur.lastrowid
        # Запишем заготовки ответов (по одному на участника)
        for uid in members:
            conn.execute(
                "INSERT INTO updates (standup_id, tg_id, answered) VALUES (?, ?, 0)",
                (standup_id, uid),
            )

    # Рассылаем приглашения с ForceReply
    text = (
        f"🕒 Дэйлик команды «{team['name']}»\n\n"
        "Ответьте одним сообщением на этот запрос:\n"
        "— Что делал вчера?\n"
        "— Что планируешь сегодня?\n"
        "— Есть ли блокеры?\n\n"
        "Можно кратко в одном сообщении."
    )
    # Храним message_id не обязательно — ловим ответы по reply_to + today & open standup
    for uid in members:
        try:
            await app.bot.send_message(
                chat_id=uid,
                text=text,
                reply_markup=ForceReply(selective=True),
            )
        except Exception:
            pass  # пользователь мог закрыть ЛС

    # Планируем повторное напоминание и итоговую сводку
    remind_key = f"remind_{standup_id}"
    summary_key = f"summary_{standup_id}"

    app.job_queue.run_once(
        callback=remind_unanswered,
        when=timedelta(minutes=REMIND_AFTER_MIN),
        name=remind_key,
        data={"standup_id": standup_id, "team_id": team_id},
    )
    app.job_queue.run_once(
        callback=post_summary,
        when=timedelta(minutes=SUMMARY_AFTER_MIN),
        name=summary_key,
        data={"standup_id": standup_id, "team_id": team_id},
    )

    with conn:
        conn.execute(
            "UPDATE standups SET remind_job_key=?, summary_job_key=? WHERE id=?",
            (remind_key, summary_key, standup_id),
        )

async def remind_unanswered(ctx: ContextTypes.DEFAULT_TYPE):
    standup_id = ctx.job.data["standup_id"]
    team_id = ctx.job.data["team_id"]
    conn = db()
    team = conn.execute("SELECT name FROM teams WHERE id=?", (team_id,)).fetchone()
    rows = conn.execute(
        "SELECT tg_id FROM updates WHERE standup_id=? AND answered=0",
        (standup_id,),
    ).fetchall()
    if not rows:
        return
    text = (
        f"⏰ Напоминание по дэйлику «{team['name']}».\n"
        "Пожалуйста, ответьте на предыдущий запрос (реплаем)."
    )
    for r in rows:
        try:
            await ctx.application.bot.send_message(r["tg_id"], text)
        except Exception:
            pass

async def post_summary(ctx: ContextTypes.DEFAULT_TYPE):
    standup_id = ctx.job.data["standup_id"]
    team_id = ctx.job.data["team_id"]

    conn = db()
    team = conn.execute("SELECT name, managers_json FROM teams WHERE id=?", (team_id,)).fetchone()
    managers = json.loads(team["managers_json"])

    # Собираем участников с именами
    members = conn.execute(
        """
        SELECT u.tg_id, u.name,
               COALESCE(upd.text, '') AS text, upd.answered AS answered
        FROM team_members tm
        JOIN users u ON u.tg_id = tm.tg_id
        LEFT JOIN updates upd ON upd.tg_id = tm.tg_id AND upd.standup_id=?
        WHERE tm.team_id=?
        ORDER BY u.name COLLATE NOCASE
        """,
        (standup_id, team_id),
    ).fetchall()

    # Формируем сводку
    lines = [f"🧾 Итоги дэйлика «{team['name']}»:"]
    for r in members:
        status = "✅" if r["answered"] else "❌"
        body = (r["text"] or "").strip()
        if not body:
            body = "_нет ответа_"
        lines.append(f"{status} <b>{r['name']}</b>\n{body}")

    summary = "\n\n".join(lines)

    # Отправляем всем участникам + менеджерам
    sent_to = set()
    for r in members:
        sent_to.add(r["tg_id"])
        try:
            await ctx.application.bot.send_message(
                chat_id=r["tg_id"], text=summary, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
    for mid in managers:
        if mid in sent_to:
            continue
        try:
            await ctx.application.bot.send_message(
                chat_id=mid, text=summary, parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

# ---------- ОБРАБОТКА ОТВЕТОВ ----------
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Принимаем ответы только как reply на бота.
    Берём последний standup этой команды за сегодня, помечаем answered=1 и сохраняем текст.
    Так как пользователь может состоять в нескольких командах, возможна неоднозначность.
    Решение: ищем все открытые standup'ы за сегодня, пишем ответ во все, где ожидался ответ.
    """
    msg = update.effective_message
    if not msg or not msg.reply_to_message or msg.from_user.is_bot:
        return

    uid = update.effective_user.id
    text = msg.text or msg.caption or ""
    if not text.strip():
        return

    conn = db()
    # Находим все standup'ы СЕГОДНЯ, где от этого юзера ещё нет ответа
    # Итерируем по всем его командам
    teams = conn.execute(
        "SELECT team_id FROM team_members WHERE tg_id=?",
        (uid,),
    ).fetchall()
    updated_any = False
    for trow in teams:
        team_id = trow["team_id"]
        team = conn.execute("SELECT tz FROM teams WHERE id=?", (team_id,)).fetchone()
        if not team:
            continue
        today = today_in_tz(team["tz"]).isoformat()
        st = conn.execute(
            "SELECT id FROM standups WHERE team_id=? AND date_iso=? ORDER BY id DESC LIMIT 1",
            (team_id, today),
        ).fetchone()
        if not st:
            continue
        upd = conn.execute(
            "SELECT id, answered FROM updates WHERE standup_id=? AND tg_id=?",
            (st["id"], uid),
        ).fetchone()
        if not upd or upd["answered"] == 1:
            continue
        with conn:
            conn.execute(
                "UPDATE updates SET text=?, created_utc=?, answered=1 WHERE id=?",
                (text.strip(), now_utc().isoformat(), upd["id"]),
            )
        updated_any = True

    if updated_any:
        await reply(update, "Принято. Спасибо!")
    else:
        # Либо нет активного дэйлика, либо уже отвечали
        await reply(update, "Ответ сохранён или активных дэйликов нет.")

# ---------- BOOTSTRAP ----------
async def restore_jobs(app: Application):
    """При старте восстанавливаем ежедневные задачи по всем командам с назначенным временем."""
    conn = db()
    teams = conn.execute(
        "SELECT id FROM teams WHERE reminder_time IS NOT NULL"
    ).fetchall()
    for r in teams:
        await reschedule_daily_job(app, r["id"])

def main():
    if not BOT_TOKEN:
        raise SystemExit("Установите BOT_TOKEN в окружении.")
    init_db()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("create_team", cmd_create_team))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("my_teams", cmd_my_teams))
    app.add_handler(CommandHandler("set_time", cmd_set_time))
    app.add_handler(CommandHandler("standup_now", cmd_standup_now))

    # Любые текстовые ответы-реплаи
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_message))

    async def _run():
        await restore_jobs(app)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        print("Bot started.")
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    asyncio.run(_run())

if __name__ == "__main__":
    main()