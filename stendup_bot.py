# standupbuddy_bot_menu.py
# python-telegram-bot==21.6
# Функционал: команды/команды через меню и кнопки
# — Создание команды
# — Приглашение/вступление по коду
# — Назначение ежедневного времени дэйлика (TZ)
# — Запуск дэйлика, сбор ответов, повторное напоминание, итоговая сводка
# — Весь UX через Inline-кнопки/мастер-диалоги

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
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ForceReply,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

DB_PATH = "dailybot.db"
BOT_TOKEN = os.getenv("BOT_TOKEN")

REMIND_AFTER_MIN = 10
SUMMARY_AFTER_MIN = 30

# --- Conversation states ---
(
    S_MENU,
    S_CREATE_TEAM_NAME,
    S_JOIN_CODE,
    S_SET_TIME_TEAM,
    S_SET_TIME_HHMM,
    S_SET_TIME_TZ,
    S_STANDUP_TEAM,
) = range(7)

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
            reminder_time   TEXT,
            managers_json   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS team_members (
            team_id INTEGER NOT NULL,
            tg_id   INTEGER NOT NULL,
            UNIQUE(team_id, tg_id)
        );

        CREATE TABLE IF NOT EXISTS standups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id         INTEGER NOT NULL,
            date_iso        TEXT NOT NULL,
            started_utc     TEXT NOT NULL,
            remind_job_key  TEXT,
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


# ---------- HELPERS ----------

def gen_invite_code(n=8):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


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


def get_user_name(u: Update):
    user = u.effective_user
    if not user:
        return "Unknown"
    full = " ".join(x for x in [user.first_name, user.last_name] if x)
    return full or (user.username or str(user.id))


# ---------- UI ----------

def main_menu(uid: int) -> InlineKeyboardMarkup:
    """Показываем только релевантные кнопки.
    Логика:
    — Если пользователь ни в одной команде: только «Создать» и «Вступить».
    — Если есть команды, но нет прав менеджера: «Мои команды» и «Вступить».
    — Если есть команды, где он менеджер: добавляем «Назначить время» и «Запустить дэйлик».
    """
    conn = db()
    rows = conn.execute(
        "SELECT t.id, t.managers_json FROM teams t JOIN team_members m ON m.team_id=t.id WHERE m.tg_id=?",
        (uid,),
    ).fetchall()
    has_teams = len(rows) > 0
    manager_teams = [r for r in rows if uid in json.loads(r["managers_json"]) ]

    buttons = []
    # Базовые CTA
    buttons.append([InlineKeyboardButton("➕ Создать команду", callback_data="m:create")])
    buttons.append([InlineKeyboardButton("🔗 Вступить по коду", callback_data="m:join")])

    if has_teams:
        buttons.insert(1, [InlineKeyboardButton("👥 Мои команды", callback_data="m:teams")])
    if manager_teams:
        buttons.append([InlineKeyboardButton("⏰ Назначить время", callback_data="m:settime")])
        buttons.append([InlineKeyboardButton("▶️ Запустить дэйлик", callback_data="m:standup")])

    return InlineKeyboardMarkup(buttons)


async def show_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str = "Главное меню"):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=main_menu(update.effective_user.id))
    else:
        await update.effective_message.reply_text(text, reply_markup=main_menu(update.effective_user.id))


# ---------- JOBS ----------

async def reschedule_daily_job(app: Application, team_id: int):
    job_name = f"daily_{team_id}"
    current = app.job_queue.get_jobs_by_name(job_name)
    for j in current:
        j.schedule_removal()

    conn = db()
    team = conn.execute("SELECT reminder_time, tz FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team or not team["reminder_time"]:
        return
    hhmm = parse_hhmm(team["reminder_time"])
    tz = pytz.timezone(team["tz"])

    app.job_queue.run_daily(
        callback=daily_job_callback,
        time=hhmm,
        days=(0,1,2,3,4,5,6),
        name=job_name,
        data={"team_id": team_id},
        tzinfo=tz,
    )


async def daily_job_callback(ctx: ContextTypes.DEFAULT_TYPE):
    team_id = ctx.job.data["team_id"]
    await start_standup(ctx.application, team_id)


async def start_standup(app: Application, team_id: int, manual: bool=False):
    conn = db()
    team = conn.execute("SELECT id, name, tz FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        return
    tz_name = team["tz"]
    today = today_in_tz(tz_name).isoformat()

    existed = conn.execute("SELECT id FROM standups WHERE team_id=? AND date_iso=?", (team_id, today)).fetchone()
    if existed and not manual:
        return

    members = [r["tg_id"] for r in conn.execute("SELECT tg_id FROM team_members WHERE team_id=?", (team_id,)).fetchall()]
    if not members:
        return

    with conn:
        cur = conn.execute(
            "INSERT INTO standups (team_id, date_iso, started_utc) VALUES (?, ?, ?)",
            (team_id, today, now_utc().isoformat()),
        )
        standup_id = cur.lastrowid
        for uid in members:
            conn.execute("INSERT INTO updates (standup_id, tg_id, answered) VALUES (?, ?, 0)", (standup_id, uid))

    text = (
        f"🕒 Дэйлик команды «{team['name']}»\n\n"
        "Ответьте одним сообщением на этот запрос (реплаем):\n"
        "— Что делал вчера?\n— Что планируешь сегодня?\n— Есть ли блокеры?"
    )
    for uid in members:
        try:
            await app.bot.send_message(chat_id=uid, text=text, reply_markup=ForceReply(selective=True))
        except Exception:
            pass

    remind_key = f"remind_{standup_id}"
    summary_key = f"summary_{standup_id}"

    app.job_queue.run_once(remind_unanswered, when=timedelta(minutes=REMIND_AFTER_MIN), name=remind_key, data={"standup_id": standup_id, "team_id": team_id})
    app.job_queue.run_once(post_summary, when=timedelta(minutes=SUMMARY_AFTER_MIN), name=summary_key, data={"standup_id": standup_id, "team_id": team_id})

    with conn:
        conn.execute("UPDATE standups SET remind_job_key=?, summary_job_key=? WHERE id=?", (remind_key, summary_key, standup_id))


async def remind_unanswered(ctx: ContextTypes.DEFAULT_TYPE):
    standup_id = ctx.job.data["standup_id"]
    team_id = ctx.job.data["team_id"]
    conn = db()
    team = conn.execute("SELECT name FROM teams WHERE id=?", (team_id,)).fetchone()
    rows = conn.execute("SELECT tg_id FROM updates WHERE standup_id=? AND answered=0", (standup_id,)).fetchall()
    if not rows:
        return
    text = f"⏰ Напоминание по дэйлику «{team['name']}». Пожалуйста, ответьте реплаем."
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
    managers = json.loads(team["managers_json"]) if team else []

    members = conn.execute(
        """
        SELECT u.tg_id, u.name, COALESCE(upd.text, '') AS text, upd.answered AS answered
        FROM team_members tm
        JOIN users u ON u.tg_id = tm.tg_id
        LEFT JOIN updates upd ON upd.tg_id = tm.tg_id AND upd.standup_id=?
        WHERE tm.team_id=?
        ORDER BY u.name COLLATE NOCASE
        """,
        (standup_id, team_id),
    ).fetchall()

    lines = [f"🧾 Итоги дэйлика «{team['name']}»:"]
    for r in members:
        status = "✅" if r["answered"] else "❌"
        body = (r["text"] or "").strip() or "_нет ответа_"
        lines.append(f"{status} <b>{r['name']}</b>\n{body}")
    summary = "\n\n".join(lines)

    sent_to = set()
    for r in members:
        sent_to.add(r["tg_id"])
        try:
            await ctx.application.bot.send_message(chat_id=r["tg_id"], text=summary, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    for mid in managers:
        if mid in sent_to:
            continue
        try:
            await ctx.application.bot.send_message(chat_id=mid, text=summary, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ---------- HANDLERS (меню) ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # регистрируем пользователя
    conn = db()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (tg_id, name) VALUES (?, ?)",
            (update.effective_user.id, get_user_name(update)),
        )
    await show_menu(update, ctx, "Привет! Это StandupBuddy. Выбирай действие:")
    return S_MENU


async def on_menu_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "m:create":
        # BUGFIX: ставим флаг ожидания имени, иначе on_text_flow не сработает
        ctx.user_data["await_create_team_name"] = True
        await q.edit_message_text("Название новой команды? Напишите текстом.")
        return S_CREATE_TEAM_NAME

    if data == "m:teams":
        uid = update.effective_user.id
        conn = db()
        rows = conn.execute(
            """
            SELECT t.id, t.name, t.tz, t.reminder_time, t.managers_json
            FROM teams t JOIN team_members m ON m.team_id=t.id
            WHERE m.tg_id=? ORDER BY t.id
            """,
            (uid,),
        ).fetchall()
        if not rows:
            await q.edit_message_text("Вы пока не в командах.", reply_markup=main_menu(uid))
            return S_MENU
        lines = []
        for r in rows:
            managers = json.loads(r["managers_json"]) if r["managers_json"] else []
            is_mgr = "да" if uid in managers else "нет"
            lines.append(f"ID {r['id']}: {r['name']} | TZ {r['tz']} | time {r['reminder_time'] or '—'} | менеджер: {is_mgr}")
        await q.edit_message_text("Ваши команды:\n" + "\n".join(lines), reply_markup=main_menu(uid))
        return S_MENU

    if data == "m:join":
        await q.edit_message_text("Введи инвайт‑код (например, 8 символов в верхнем регистре).")
        return S_JOIN_CODE

    if data == "m:settime":
        uid = update.effective_user.id
        conn = db()
        rows = conn.execute(
            "SELECT id, name, managers_json FROM teams t JOIN team_members m ON m.team_id=t.id WHERE m.tg_id=?",
            (uid,),
        ).fetchall()
        manager_teams = [r for r in rows if uid in json.loads(r["managers_json"]) ]
        if not manager_teams:
            await q.edit_message_text("Нет команд, где вы менеджер.", reply_markup=main_menu(uid))
            return S_MENU
        # Выбрать команду
        buttons = [[InlineKeyboardButton(f"{r['name']} (ID {r['id']})", callback_data=f"settime:{r['id']}") ] for r in manager_teams]
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back:menu")])
        await q.edit_message_text("Выберите команду для назначения времени:", reply_markup=InlineKeyboardMarkup(buttons))
        return S_SET_TIME_TEAM

    if data == "m:standup":
        uid = update.effective_user.id
        conn = db()
        rows = conn.execute(
            "SELECT id, name, managers_json FROM teams t JOIN team_members m ON m.team_id=t.id WHERE m.tg_id=?",
            (uid,),
        ).fetchall()
        manager_teams = [r for r in rows if uid in json.loads(r["managers_json"]) ]
        if not manager_teams:
            await q.edit_message_text("Нет команд, где вы менеджер.", reply_markup=main_menu(uid))
            return S_MENU
        buttons = [[InlineKeyboardButton(f"▶️ {r['name']} (ID {r['id']})", callback_data=f"standup:{r['id']}") ] for r in manager_teams]
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back:menu")])
        await q.edit_message_text("Выберите команду для запуска дэйлика сейчас:", reply_markup=InlineKeyboardMarkup(buttons))
        return S_STANDUP_TEAM

    if data.startswith("back:"):
        await show_menu(update, ctx)
        return S_MENU

    return S_MENU


async def on_settime_team_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("settime:"):
        team_id = int(q.data.split(":", 1)[1])
        ctx.user_data["settime_team_id"] = team_id
        await q.edit_message_text("Введите время в формате HH:MM (например, 10:00)")
        return S_SET_TIME_HHMM
    if q.data == "back:menu":
        await show_menu(update, ctx)
        return S_MENU
    return S_SET_TIME_TEAM


async def on_settime_hhmm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hhmm = (update.effective_message.text or "").strip()
    try:
        _ = parse_hhmm(hhmm)
    except Exception:
        await update.effective_message.reply_text("Неверный формат. Пример: 09:30. Введите ещё раз:")
        return S_SET_TIME_HHMM
    ctx.user_data["settime_hhmm"] = hhmm
    await update.effective_message.reply_text("Теперь укажите часовой пояс, например: Europe/Moscow")
    return S_SET_TIME_TZ


async def on_settime_tz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tz_name = (update.effective_message.text or "").strip()
    try:
        pytz.timezone(tz_name)
    except Exception:
        await update.effective_message.reply_text("Неизвестный TZ. Пример: Europe/Amsterdam. Введите ещё раз:")
        return S_SET_TIME_TZ

    team_id = ctx.user_data.get("settime_team_id")
    hhmm = ctx.user_data.get("settime_hhmm")

    uid = update.effective_user.id
    conn = db()
    team = conn.execute("SELECT name, managers_json FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        await update.effective_message.reply_text("Команда не найдена.")
        await show_menu(update, ctx)
        return S_MENU
    if uid not in json.loads(team["managers_json"]):
        await update.effective_message.reply_text("Только менеджер может менять время.")
        await show_menu(update, ctx)
        return S_MENU

    with conn:
        conn.execute("UPDATE teams SET reminder_time=?, tz=? WHERE id=?", (hhmm, tz_name, team_id))
    await reschedule_daily_job(ctx.application, team_id)

    await update.effective_message.reply_text(f"Ок! Для команды «{team['name']}» время дэйлика: {hhmm} ({tz_name}).")
    await show_menu(update, ctx)
    return S_MENU


async def on_standup_team_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("standup:"):
        team_id = int(q.data.split(":", 1)[1])
        await start_standup(ctx.application, team_id, manual=True)
        await q.edit_message_text("Дэйлик запущен. Сообщения отправлены участникам.", reply_markup=main_menu(update.effective_user.id))
        return S_MENU
    if q.data == "back:menu":
        await show_menu(update, ctx)
        return S_MENU
    return S_STANDUP_TEAM


async def on_text_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get("state")
    # обработка общих шагов диалога
    if ctx.user_data.get("await_create_team_name"):
        name = (update.effective_message.text or "").strip()
        code = gen_invite_code()
        manager_id = update.effective_user.id
        conn = db()
        with conn:
            cur = conn.execute(
                "INSERT INTO teams (name, invite_code, tz, reminder_time, managers_json) VALUES (?, ?, 'UTC', NULL, ?)",
                (name, code, json.dumps([manager_id])),
            )
            team_id = cur.lastrowid
            conn.execute("INSERT OR IGNORE INTO team_members (team_id, tg_id) VALUES (?, ?)", (team_id, manager_id))
        ctx.user_data.pop("await_create_team_name", None)
        await update.effective_message.reply_text(
            f"Команда создана!\nID: {team_id}\nИнвайт‑код: {code}\nНазначьте время через меню: ⏰ Назначить время",
        )
        await show_menu(update, ctx)
        return S_MENU

    if ctx.user_data.get("await_join_code"):
        code = (update.effective_message.text or "").strip().upper()
        conn = db()
        team = conn.execute("SELECT id, name FROM teams WHERE invite_code=?", (code,)).fetchone()
        if not team:
            await update.effective_message.reply_text("Неверный код. Попробуйте снова или вернитесь в меню /start")
            return S_JOIN_CODE
        with conn:
            conn.execute("INSERT OR IGNORE INTO team_members (team_id, tg_id) VALUES (?, ?)", (team["id"], update.effective_user.id))
        ctx.user_data.pop("await_join_code", None)
        await update.effective_message.reply_text(f"Ок! Вы в команде «{team['name']}» (ID {team['id']}).")
        await show_menu(update, ctx)
        return S_MENU

    # если это ответ на ForceReply (дэйлик)
    msg = update.effective_message
    if msg and msg.reply_to_message and not msg.from_user.is_bot:
        uid = update.effective_user.id
        text = msg.text or msg.caption or ""
        if text.strip():
            conn = db()
            teams = conn.execute("SELECT team_id FROM team_members WHERE tg_id=?", (uid,)).fetchall()
            updated_any = False
            for trow in teams:
                team_id = trow["team_id"]
                team = conn.execute("SELECT tz FROM teams WHERE id=?", (team_id,)).fetchone()
                if not team:
                    continue
                today = today_in_tz(team["tz"]).isoformat()
                st = conn.execute("SELECT id FROM standups WHERE team_id=? AND date_iso=? ORDER BY id DESC LIMIT 1", (team_id, today)).fetchone()
                if not st:
                    continue
                upd = conn.execute("SELECT id, answered FROM updates WHERE standup_id=? AND tg_id=?", (st["id"], uid)).fetchone()
                if not upd or upd["answered"] == 1:
                    continue
                with conn:
                    conn.execute("UPDATE updates SET text=?, created_utc=?, answered=1 WHERE id=?", (text.strip(), now_utc().isoformat(), upd["id"]))
                updated_any = True
            if updated_any:
                await msg.reply_text("Принято. Спасибо!")
            else:
                await msg.reply_text("Ответ сохранён или активных дэйликов нет.")
        return ConversationHandler.END

    return ConversationHandler.END


# --- Callbacks to switch states when pressing menu buttons ---
async def start_create_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["await_create_team_name"] = True
    await q.edit_message_text("Название новой команды?")
    return S_CREATE_TEAM_NAME


async def start_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["await_join_code"] = True
    await q.edit_message_text("Введи инвайт‑код:")
    return S_JOIN_CODE


# ---------- ROUTING ----------

def build_app() -> Application:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            S_MENU: [
                CallbackQueryHandler(on_menu_click),
            ],
            S_CREATE_TEAM_NAME: [MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_flow)],
            S_JOIN_CODE: [MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_flow)],
            S_SET_TIME_TEAM: [CallbackQueryHandler(on_settime_team_choice)],
            S_SET_TIME_HHMM: [MessageHandler(filters.TEXT & (~filters.COMMAND), on_settime_hhmm)],
            S_SET_TIME_TZ: [MessageHandler(filters.TEXT & (~filters.COMMAND), on_settime_tz)],
            S_STANDUP_TEAM: [CallbackQueryHandler(on_standup_team_choice)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )

    app.add_handler(conv)

    # запасной обработчик текстов (ответы на ForceReply)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_flow))

    return app


async def restore_jobs(app: Application):
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
