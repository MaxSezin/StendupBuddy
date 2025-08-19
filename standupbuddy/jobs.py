import json
from datetime import timedelta

from telegram import ForceReply
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from .config import REMIND_AFTER_MIN, SUMMARY_AFTER_MIN
from .db import db
from .utils import parse_hhmm, tz_from_str, today_in_tz, now_utc, parse_reminder_days


async def remove_daily_job(app: Application, team_id: int):
    for j in app.job_queue.get_jobs_by_name(f"daily_{team_id}"):
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
        time=hhmm, days=days, name=f"daily_{team_id}", data={"team_id": team_id}, tzinfo=tzinfo
    )


async def daily_job_callback(ctx: ContextTypes.DEFAULT_TYPE):
    await start_standup(ctx.application, ctx.job.data["team_id"])


async def start_standup(app: Application, team_id: int, manual: bool = False):
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
        cur = conn.execute("INSERT INTO standups (team_id, date_iso, started_utc) VALUES (?, ?, ?)", (team_id, today, now_utc().isoformat()))
        standup_id = cur.lastrowid
        for uid in members:
            conn.execute("INSERT INTO updates (standup_id, tg_id, answered) VALUES (?, ?, 0)", (standup_id, uid))
    text = (f"🕒 Дэйлик команды «{team['name']}»\n\n"
            "Ответьте одним сообщением:\n— Что делал вчера?\n— Что планируешь сегодня?\n— Есть ли блокеры?")
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
        if r["answered"]:
            status = "✅"
            body = (r["text"] or "").strip() or "_пустой ответ_"
        else:
            status = "❌"
            body = "— _не ответил_"
        lines.append(f"{status} <b>{r['name']}</b>\n{body}")
    summary = "\n\n".join(lines)
    
    # Send to all members
    sent_to = set()
    for r in members:
        sent_to.add(r["tg_id"])
        try:
            await ctx.application.bot.send_message(chat_id=r["tg_id"], text=summary, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    
    # Send to managers (including those who are also members)
    for mid in managers:
        try:
            await ctx.application.bot.send_message(chat_id=mid, text=summary, parse_mode=ParseMode.HTML)
        except Exception:
            pass


