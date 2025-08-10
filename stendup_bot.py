# standupbuddy_bot.py
# StandupBuddy — меню группы, участники, выход/удаление, UTC±N и кнопки "Отмена"
# python-telegram-bot==21.6, pytz
#
# Запуск:
#   pip install -r requirements.txt
#   export BOT_TOKEN=...    # токен бота из @BotFather
#   python standupbuddy_bot.py

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

# Conversation states
(
    S_MENU,
    S_CREATE_TEAM_NAME,
    S_JOIN_CODE,
    S_GROUP_SELECT,
    S_GROUP_MENU,
    S_SET_TIME_HHMM,
    S_SET_TIME_TZ,   # UTC±N buttons
    S_SET_SCHEDULE,
    S_REMOVE_MEMBER_SELECT,
) = range(9)

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
            reminder_days   TEXT,              -- JSON-список дней недели: 0=Mon..6=Sun
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


def tz_from_str(tz_str: str):
    if tz_str and tz_str.upper().startswith("UTC"):
        sign = 1
        rest = tz_str[3:]
        if rest.startswith("+"):
            sign = 1
            rest = rest[1:]
        elif rest.startswith("-"):
            sign = -1
            rest = rest[1:]
        try:
            hours = int(rest)
            return pytz.FixedOffset(sign * hours * 60)
        except Exception:
            pass
    try:
        return pytz.timezone(tz_str)
    except Exception:
        return pytz.UTC


def today_in_tz(tz_str: str):
    tz = tz_from_str(tz_str)
    return datetime.now(tz).date()


def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def get_user_name(u: Update):
    user = u.effective_user
    if not user:
        return "Unknown"
    full = " ".join(x for x in [user.first_name, user.last_name] if x)
    return full or (user.username or str(user.id))


def parse_reminder_days(raw: str | None) -> tuple[int, ...]:
    if not raw or raw.strip() == "":
        return tuple(range(7))
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return tuple(int(x) for x in data)
    except Exception:
        pass
    try:
        return tuple(int(x) for x in raw.split(",") if x != "")
    except Exception:
        return tuple(range(7))


def days_to_label(days: tuple[int, ...]) -> str:
    days = sorted(set(int(d) for d in days))
    if tuple(days) == tuple(range(7)): return "каждый день"
    if tuple(days) == tuple(range(5)): return "по будням"
    if tuple(days) == (5, 6):          return "по выходным"
    names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    return ", ".join(names[d] for d in days)


# ---------- UI ----------

def main_menu(uid: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("➕ Создать команду", callback_data="m:create")],
        [InlineKeyboardButton("🔗 Вступить по коду", callback_data="m:join")],
        [InlineKeyboardButton("👥 Мои команды — выбрать", callback_data="m:teams")],
    ]
    return InlineKeyboardMarkup(buttons)


def group_menu_keyboard(team_row, is_manager: bool, self_id: int) -> InlineKeyboardMarkup:
    has_schedule = bool(team_row["reminder_time"])
    buttons = []
    if has_schedule:
        buttons.append([InlineKeyboardButton("📄 Посмотреть расписание", callback_data=f"gm:view:{team_row['id']}")])
        if is_manager:
            buttons.append([InlineKeyboardButton("✏️ Редактировать расписание", callback_data=f"gm:edit:{team_row['id']}")])
            buttons.append([InlineKeyboardButton("🗑 Удалить расписание", callback_data=f"gm:del:{team_row['id']}")])
    else:
        if is_manager:
            buttons.append([InlineKeyboardButton("➕ Создать расписание", callback_data=f"gm:edit:{team_row['id']}")])
    buttons.append([InlineKeyboardButton("👥 Участники", callback_data=f"gm:members:{team_row['id']}")])
    buttons.append([InlineKeyboardButton("↩️ Выйти из группы", callback_data=f"gm:leave:{team_row['id']}")])
    if is_manager:
        buttons.append([InlineKeyboardButton("❌ Удалить участника…", callback_data=f"gm:rmembers:{team_row['id']}")])
    buttons.append([InlineKeyboardButton("◀️ К списку групп", callback_data="back:teams")])
    buttons.append([InlineKeyboardButton("🏠 В меню", callback_data="back:menu")])
    return InlineKeyboardMarkup(buttons)


def cancel_kb_to_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back:menu")]])


def cancel_kb_to_group():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="back:group")]])


def team_choice_keyboard(uid: int) -> InlineKeyboardMarkup:
    conn = db()
    rows = conn.execute(
        "SELECT t.id, t.name FROM teams t JOIN team_members m ON m.team_id=t.id WHERE m.tg_id=? ORDER BY t.id",
        (uid,),
    ).fetchall()
    if not rows:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="back:menu")]])
    buttons = [[InlineKeyboardButton(f"{r['name']} (ID {r['id']})", callback_data=f"g:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("🏠 В меню", callback_data="back:menu")])
    return InlineKeyboardMarkup(buttons)


def tz_offset_keyboard() -> InlineKeyboardMarkup:
    offsets = list(range(-12, 15))
    rows = []
    row = []
    for off in offsets:
        label = f"UTC{off:+d}"
        row.append(InlineKeyboardButton(label, callback_data=f"tzo:{off}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back:group")])
    return InlineKeyboardMarkup(rows)


def schedule_preset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Каждый день", callback_data="sch:preset:everyday")],
        [InlineKeyboardButton("🏢 Будни (Пн–Пт)", callback_data="sch:preset:weekdays")],
        [InlineKeyboardButton("🎉 Выходные (Сб–Вс)", callback_data="sch:preset:weekends")],
        [InlineKeyboardButton("🧩 Кастомные дни…", callback_data="sch:custom:start")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back:group")],
    ])


def schedule_custom_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    rows = []
    for i, n in enumerate(names):
        mark = "✅" if i in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {n}", callback_data=f"sch:custom:toggle:{i}")])
    rows.append([
        InlineKeyboardButton("Сохранить", callback_data="sch:custom:save"),
        InlineKeyboardButton("Сброс", callback_data="sch:custom:reset"),
    ])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back:schedule")])
    return InlineKeyboardMarkup(rows)


# ---------- JOBS ----------

async def remove_daily_job(app: Application, team_id: int):
    job_name = f"daily_{team_id}"
    for j in app.job_queue.get_jobs_by_name(job_name):
        j.schedule_removal()


async def reschedule_daily_job(app: Application, team_id: int):
    await remove_daily_job(app, team_id)
    conn = db()
    team = conn.execute("SELECT reminder_time, tz, reminder_days FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team or not team["reminder_time"]:
        return
    hhmm = parse_hhmm(team["reminder_time"])
    tzinfo = tz_from_str(team["tz"])
    days = parse_reminder_days(team["reminder_days"])
    app.job_queue.run_daily(
        callback=daily_job_callback,
        time=hhmm,
        days=days,
        name=f"daily_{team_id}",
        data={"team_id": team_id},
        tzinfo=tzinfo,
    )


async def daily_job_callback(ctx: ContextTypes.DEFAULT_TYPE):
    team_id = ctx.job.data["team_id"]
    await start_standup(ctx.application, team_id)


async def start_standup(app: Application, team_id: int, manual: bool=False):
    conn = db()
    team = conn.execute("SELECT id, name, tz FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        return
    tz_str = team["tz"]
    today = today_in_tz(tz_str).isoformat()
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
        "Ответьте одним сообщением:\n"
        "— Что делал вчера?\n— Что планируешь сегодня?\n— Есть ли блокеры?"
    )
    for uid in members:
        try:
            await app.bot.send_message(chat_id=uid, text=text, reply_markup=ForceReply(selective=True))
        except Exception:
            pass
    app.job_queue.run_once(remind_unanswered, when=timedelta(minutes=REMIND_AFTER_MIN), name=f"remind_{standup_id}", data={"standup_id": standup_id, "team_id": team_id})
    app.job_queue.run_once(post_summary, when=timedelta(minutes=SUMMARY_AFTER_MIN), name=f"summary_{standup_id}", data={"standup_id": standup_id, "team_id": team_id})


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


# ---------- HANDLERS (меню и команды) ----------

async def show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str | None = None):
    msg = text or "Привет! Это StandupBuddy. Выбери действие:"
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=main_menu(update.effective_user.id))
    else:
        await update.effective_message.reply_text(msg, reply_markup=main_menu(update.effective_user.id))


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = db()
    with conn:
        conn.execute("INSERT OR REPLACE INTO users (tg_id, name) VALUES (?, ?)", (update.effective_user.id, get_user_name(update)))
    await show_main_menu(update, ctx)
    return S_MENU


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "StandupBuddy автоматизирует дэйлики.\n"
        "/start — меню, /cancel — выйти из мастера, /health — проверка."
    )
    return S_MENU


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for k in ("await_create_team_name","await_join_code","group_id","settime_hhmm","settime_tz","settime_days"):
        ctx.user_data.pop(k, None)
    await update.effective_message.reply_text("Отменил. Возвращаю в меню.")
    await show_main_menu(update, ctx)
    return S_MENU


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        conn = db()
        conn.execute("SELECT 1")
        ok_db = True
    except Exception:
        ok_db = False
    jobs = ctx.application.job_queue.jobs()
    await update.effective_message.reply_text(
        f"DB: {'OK' if ok_db else 'FAIL'} | Jobs: {len(jobs)}"
    )
    return S_MENU


def team_choice_keyboard(uid: int) -> InlineKeyboardMarkup:
    conn = db()
    rows = conn.execute(
        "SELECT t.id, t.name FROM teams t JOIN team_members m ON m.team_id=t.id WHERE m.tg_id=? ORDER BY t.id",
        (uid,),
    ).fetchall()
    if not rows:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 В меню", callback_data="back:menu")]])
    buttons = [[InlineKeyboardButton(f"{r['name']} (ID {r['id']})", callback_data=f"g:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("🏠 В меню", callback_data="back:menu")])
    return InlineKeyboardMarkup(buttons)


async def on_menu_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "m:create":
        ctx.user_data["await_create_team_name"] = True
        await q.edit_message_text("Название новой команды? Напишите текстом.", reply_markup=cancel_kb_to_menu())
        return S_CREATE_TEAM_NAME

    if data in ("m:teams", "back:teams"):
        await q.edit_message_text("Выберите группу:", reply_markup=team_choice_keyboard(update.effective_user.id))
        return S_GROUP_SELECT

    if data.startswith("g:"):
        team_id = int(data.split(":",1)[1])
        conn = db()
        team = conn.execute("SELECT id, name, tz, reminder_time, reminder_days, managers_json FROM teams WHERE id=?", (team_id,)).fetchone()
        if not team:
            await q.edit_message_text("Команда не найдена.", reply_markup=team_choice_keyboard(update.effective_user.id))
            return S_GROUP_SELECT
        ctx.user_data["group_id"] = team_id
        is_mgr = update.effective_user.id in json.loads(team["managers_json"])
        await q.edit_message_text(
            f"Команда «{team['name']}» (ID {team_id})", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)
        )
        return S_GROUP_MENU

    if data == "m:join":
        ctx.user_data["await_join_code"] = True
        await q.edit_message_text("Введи инвайт‑код:", reply_markup=cancel_kb_to_menu())
        return S_JOIN_CODE

    if data == "back:menu":
        await show_main_menu(update, ctx)
        return S_MENU

    return S_MENU


async def on_group_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    team_id = ctx.user_data.get("group_id")
    if not team_id:
        await show_main_menu(update, ctx, "Группа не выбрана.")
        return S_MENU

    conn = db()
    team = conn.execute("SELECT id, name, tz, reminder_time, reminder_days, managers_json FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        await q.edit_message_text("Команда не найдена.", reply_markup=team_choice_keyboard(update.effective_user.id))
        return S_GROUP_SELECT
    managers = json.loads(team["managers_json"])
    is_mgr = update.effective_user.id in managers

    if data == "back:group":
        await q.edit_message_text(f"Команда «{team['name']}» (ID {team_id})", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id))
        return S_GROUP_MENU

    if data == f"gm:view:{team_id}":
        if team["reminder_time"]:
            label = days_to_label(parse_reminder_days(team["reminder_days"]))
            txt = f"✅ Расписание:\nВремя: {team['reminder_time']}\nTZ: {team['tz']}\nДни: {label}"
        else:
            txt = "Расписание ещё не создано."
        await q.edit_message_text(txt, reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id))
        return S_GROUP_MENU

    if data == f"gm:edit:{team_id}":
        if not is_mgr:
            await q.edit_message_text("Только менеджер может менять расписание.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id))
            return S_GROUP_MENU
        ctx.user_data["settime_hhmm"] = None
        await q.edit_message_text("Введите время в формате HH:MM (например, 10:00)", reply_markup=cancel_kb_to_group())
        return S_SET_TIME_HHMM

    if data == f"gm:del:{team_id}":
        if not is_mgr:
            await q.edit_message_text("Только менеджер может удалять расписание.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id))
            return S_GROUP_MENU
        with conn:
            conn.execute("UPDATE teams SET reminder_time=NULL, reminder_days=NULL WHERE id=?", (team_id,))
        await remove_daily_job(ctx.application, team_id)
        new_team = conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
        await q.edit_message_text("Расписание удалено.", reply_markup=group_menu_keyboard(new_team, is_mgr, update.effective_user.id))
        return S_GROUP_MENU

    if data == f"gm:members:{team_id}":
        members = conn.execute(
            "SELECT u.tg_id, u.name FROM team_members tm JOIN users u ON u.tg_id=tm.tg_id WHERE tm.team_id=? ORDER BY u.name COLLATE NOCASE",
            (team_id,)
        ).fetchall()
        names = []
        for m in members:
            mark = " (менеджер)" if m["tg_id"] in managers else ""
            names.append(f"• {m['name']}{mark}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back:group")]])
        await q.edit_message_text("👥 Участники:\n" + ("\n".join(names) if names else "— никого"), reply_markup=kb)
        return S_GROUP_MENU

    if data == f"gm:leave:{team_id}":
        if is_mgr and len(managers) == 1 and managers[0] == update.effective_user.id:
            await q.edit_message_text("Нельзя выйти: вы единственный менеджер. Назначьте другого менеджера и попробуйте снова.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id))
            return S_GROUP_MENU
        with conn:
            conn.execute("DELETE FROM team_members WHERE team_id=? AND tg_id=?", (team_id, update.effective_user.id))
            if is_mgr:
                managers = [m for m in managers if m != update.effective_user.id]
                conn.execute("UPDATE teams SET managers_json=? WHERE id=?", (json.dumps(managers), team_id))
        ctx.user_data.pop("group_id", None)
        await q.edit_message_text("Вы вышли из группы.", reply_markup=team_choice_keyboard(update.effective_user.id))
        return S_GROUP_SELECT

    if data == f"gm:rmembers:{team_id}":
        if not is_mgr:
            await q.edit_message_text("Только менеджер может удалять участников.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id))
            return S_GROUP_MENU
        members = conn.execute(
            "SELECT u.tg_id, u.name FROM team_members tm JOIN users u ON u.tg_id=tm.tg_id WHERE tm.team_id=? ORDER BY u.name COLLATE NOCASE",
            (team_id,)
        ).fetchall()
        btns = []
        for m in members:
            if m["tg_id"] == update.effective_user.id:
                continue
            btns.append([InlineKeyboardButton(f"Удалить {m['name']}", callback_data=f"rm:{team_id}:{m['tg_id']}")])
        btns.append([InlineKeyboardButton("◀️ Назад", callback_data="back:group")])
        await q.edit_message_text("Кого удалить?", reply_markup=InlineKeyboardMarkup(btns))
        return S_REMOVE_MEMBER_SELECT

    if data == "back:teams":
        await q.edit_message_text("Выберите группу:", reply_markup=team_choice_keyboard(update.effective_user.id))
        return S_GROUP_SELECT

    if data == "back:menu":
        await show_main_menu(update, ctx)
        return S_MENU

    return S_GROUP_MENU


async def on_remove_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "back:group":
        return await on_group_menu(update, ctx)
    if not data.startswith("rm:"):
        return S_REMOVE_MEMBER_SELECT
    _, team_id_s, user_id_s = data.split(":")
    team_id = int(team_id_s); user_id = int(user_id_s)
    conn = db()
    team = conn.execute("SELECT managers_json, name FROM teams WHERE id=?", (team_id,)).fetchone()
    managers = json.loads(team["managers_json"]) if team else []
    if user_id in managers and len(managers) == 1:
        await q.edit_message_text("Нельзя удалить единственного менеджера.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back:group")]]))
        return S_GROUP_MENU
    with conn:
        conn.execute("DELETE FROM team_members WHERE team_id=? AND tg_id=?", (team_id, user_id))
        if user_id in managers:
            managers = [m for m in managers if m != user_id]
            conn.execute("UPDATE teams SET managers_json=? WHERE id=?", (json.dumps(managers), team_id))
    await q.edit_message_text("Участник удалён.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back:group")]]))
    return S_GROUP_MENU


async def on_settime_hhmm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hhmm = (update.effective_message.text or "").strip()
    try:
        _ = parse_hhmm(hhmm)
    except Exception:
        await update.effective_message.reply_text("Неверный формат. Пример: 09:30. Введите ещё раз:", reply_markup=cancel_kb_to_group())
        return S_SET_TIME_HHMM
    ctx.user_data["settime_hhmm"] = hhmm
    await update.effective_message.reply_text(
        "Выбери смещение часового пояса (UTC±N):", reply_markup=tz_offset_keyboard()
    )
    return S_SET_TIME_TZ


async def on_tz_offset_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data.startswith("tzo:"):
        off = int(data.split(":", 1)[1])
        tz_name = f"UTC{off:+d}"
        ctx.user_data["settime_tz"] = tz_name
        await q.edit_message_text(
            f"Часовой пояс: {tz_name}. Выберите расписание:", reply_markup=schedule_preset_keyboard()
        )
        return S_SET_SCHEDULE
    if data == "back:group":
        return await on_group_menu(update, ctx)


async def on_settime_tz_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tz_name = (update.effective_message.text or "").strip()
    try:
        _ = tz_from_str(tz_name)
    except Exception:
        await update.effective_message.reply_text("Не понял таймзону. Используй формат UTC+3 или выбери кнопку.", reply_markup=cancel_kb_to_group())
        return S_SET_TIME_TZ
    ctx.user_data["settime_tz"] = tz_name
    await update.effective_message.reply_text("Выберите расписание запусков:", reply_markup=schedule_preset_keyboard())
    return S_SET_SCHEDULE


async def on_schedule_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    team_id = ctx.user_data.get("group_id")
    if not team_id:
        await show_main_menu(update, ctx, "Группа не выбрана.")
        return S_MENU

    def finish_save(days: tuple[int, ...]):
        hhmm = ctx.user_data.get("settime_hhmm")
        tz_name = ctx.user_data.get("settime_tz")
        uid = update.effective_user.id
        if not hhmm or not tz_name:
            return "Не хватает данных. Начните заново.", None
        conn = db()
        team = conn.execute("SELECT name, managers_json FROM teams WHERE id=?", (team_id,)).fetchone()
        if not team:
            return "Команда не найдена.", None
        if uid not in json.loads(team["managers_json"]):
            return "Только менеджер может менять расписание.", None
        days_json = json.dumps(list(days))
        with conn:
            conn.execute(
                "UPDATE teams SET reminder_time=?, tz=?, reminder_days=? WHERE id=?",
                (hhmm, tz_name, days_json, team_id),
            )
        for k in ("settime_hhmm","settime_tz","settime_days"):
            ctx.user_data.pop(k, None)
        asyncio.create_task(reschedule_daily_job(ctx.application, team_id))
        return f"Ок! Время дэйлика: {hhmm} ({tz_name}), дни: {days_to_label(days)}.", None

    if data == "sch:preset:everyday":
        msg, _ = finish_save(tuple(range(7)))
    elif data == "sch:preset:weekdays":
        msg, _ = finish_save(tuple(range(5)))
    elif data == "sch:preset:weekends":
        msg, _ = finish_save((5, 6))
    elif data.startswith("sch:custom"):
        sel = set(ctx.user_data.get("settime_days", set()))
        if data == "sch:custom:start":
            if not sel:
                sel = set(range(5))
            ctx.user_data["settime_days"] = sel
            await q.edit_message_text("Отметьте дни недели:", reply_markup=schedule_custom_keyboard(sel))
            return S_SET_SCHEDULE
        if data == "sch:custom:back":
            await q.edit_message_text("Выберите расписание:", reply_markup=schedule_preset_keyboard())
            return S_SET_SCHEDULE
        if data == "sch:custom:reset":
            ctx.user_data["settime_days"] = set()
            await q.edit_message_text("Отметьте дни недели:", reply_markup=schedule_custom_keyboard(set()))
            return S_SET_SCHEDULE
        if data == "sch:custom:save":
            days = tuple(sorted(ctx.user_data.get("settime_days", set())))
            if not days:
                await q.edit_message_text("Нужно выбрать хотя бы один день.", reply_markup=schedule_custom_keyboard(set()))
                return S_SET_SCHEDULE
            msg, _ = finish_save(days)
        if data.startswith("sch:custom:toggle:"):
            d = int(data.rsplit(":", 1)[1])
            if d in sel:
                sel.remove(d)
            else:
                sel.add(d)
            ctx.user_data["settime_days"] = sel
            await q.edit_message_text("Отметьте дни недели:", reply_markup=schedule_custom_keyboard(sel))
            return S_SET_SCHEDULE
    else:
        await q.edit_message_text("Выберите расписание:", reply_markup=schedule_preset_keyboard())
        return S_SET_SCHEDULE

    conn = db()
    team = conn.execute("SELECT id, name, tz, reminder_time, reminder_days, managers_json FROM teams WHERE id=?", (team_id,)).fetchone()
    is_mgr = update.effective_user.id in json.loads(team["managers_json"])
    await q.edit_message_text(msg, reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id))
    return S_GROUP_MENU


async def on_text_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("await_create_team_name"):
        name = (update.effective_message.text or "").strip()
        code = gen_invite_code()
        manager_id = update.effective_user.id
        conn = db()
        with conn:
            cur = conn.execute(
                "INSERT INTO teams (name, invite_code, tz, reminder_time, reminder_days, managers_json) VALUES (?, ?, 'UTC', NULL, NULL, ?)",
                (name, code, json.dumps([manager_id])),
            )
            team_id = cur.lastrowid
            conn.execute("INSERT OR IGNORE INTO team_members (team_id, tg_id) VALUES (?, ?)", (team_id, manager_id))
        ctx.user_data.pop("await_create_team_name", None)
        await update.effective_message.reply_text(
            f"Команда создана!\n"
            f"ID команды: {team_id}\n"
            f"Инвайт‑код: {code}\n"
            f"Поделитесь этим кодом коллеге.\n"
            f"Теперь выберите группу, чтобы перейти в её настройки.",
            reply_markup=team_choice_keyboard(update.effective_user.id)
        )
        return S_GROUP_SELECT

    if ctx.user_data.get("await_join_code"):
        code = (update.effective_message.text or "").strip().upper()
        conn = db()
        team = conn.execute("SELECT id, name FROM teams WHERE invite_code=?", (code,)).fetchone()
        if not team:
            await update.effective_message.reply_text("Неверный код. Попробуйте снова.", reply_markup=cancel_kb_to_menu())
            return S_JOIN_CODE
        with conn:
            conn.execute("INSERT OR IGNORE INTO team_members (team_id, tg_id) VALUES (?, ?)", (team["id"], update.effective_user.id))
        ctx.user_data.pop("await_join_code", None)
        await update.effective_message.reply_text(f"Ок! Вы в команде «{team['name']}» (ID {team['id']}). Теперь выберите группу в меню.", reply_markup=team_choice_keyboard(update.effective_user.id))
        return S_GROUP_SELECT

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


# ---------- ROUTING ----------

async def on_error(update: object, context):
    try:
        print("[ERROR]", context.error)
    except Exception:
        pass
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Упс, что-то пошло не так. Попробуйте ещё раз: /start")
        except Exception:
            pass


def build_app() -> Application:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("help", cmd_help),
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("health", cmd_health),
        ],
        states={
            S_MENU: [CallbackQueryHandler(on_menu_click, pattern=r"^(m:|back:menu|back:teams|g:)")],
            S_GROUP_SELECT: [CallbackQueryHandler(on_menu_click, pattern=r"^(g:|back:menu|m:teams)$")],
            S_GROUP_MENU: [CallbackQueryHandler(on_group_menu, pattern=r"^(gm:|back:group|back:teams|back:menu)$")],
            S_CREATE_TEAM_NAME: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_flow),
                CallbackQueryHandler(on_menu_click, pattern=r"^back:menu$"),
            ],
            S_JOIN_CODE: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_flow),
                CallbackQueryHandler(on_menu_click, pattern=r"^back:menu$"),
            ],
            S_SET_TIME_HHMM: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), on_settime_hhmm),
                CallbackQueryHandler(on_group_menu, pattern=r"^back:group$"),
            ],
            S_SET_TIME_TZ: [
                CallbackQueryHandler(on_tz_offset_pick, pattern=r"^tzo:-?\d+$|^back:group$"),
                MessageHandler(filters.TEXT & (~filters.COMMAND), on_settime_tz_manual),
            ],
            S_SET_SCHEDULE: [
                CallbackQueryHandler(on_schedule_pick, pattern=r"^(sch:|back:group|back:schedule)$"),
            ],
            S_REMOVE_MEMBER_SELECT: [
                CallbackQueryHandler(on_remove_member, pattern=r"^(rm:|back:group)$")
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("help", cmd_help),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_flow))
    app.add_error_handler(on_error)

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
