import asyncio
import json

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from .db import db
from .keyboards import (
    main_menu,
    group_menu_keyboard,
    cancel_kb_to_menu,
    cancel_kb_to_group,
    team_choice_keyboard,
    tz_offset_keyboard,
    schedule_preset_keyboard,
    schedule_custom_keyboard,
)
from .jobs import reschedule_daily_job, start_standup
from .states import (
    S_MENU,
    S_CREATE_TEAM_NAME,
    S_JOIN_CODE,
    S_GROUP_SELECT,
    S_GROUP_MENU,
    S_SET_TIME_HHMM,
    S_SET_TIME_TZ,
    S_SET_SCHEDULE,
    S_REMOVE_MEMBER_SELECT,
)
from .utils import get_user_name, parse_hhmm, tz_from_str, today_in_tz, now_utc, gen_invite_code, days_to_label, parse_reminder_days, compute_next_run_local


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
    await update.effective_message.reply_text("StandupBuddy автоматизирует дэйлики.\n/start — меню, /health — проверка.")
    return S_MENU


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        conn = db(); conn.execute("SELECT 1"); ok_db = True
    except Exception:
        ok_db = False
    await update.effective_message.reply_text(f"DB: {'OK' if ok_db else 'FAIL'} | Jobs: {len(ctx.application.job_queue.jobs())}")
    return S_MENU


async def on_menu_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); data = q.data
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
        await q.edit_message_text(f"Команда «{team['name']}» (ID {team_id})", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id))
        return S_GROUP_MENU
    if data == "m:join":
        ctx.user_data["await_join_code"] = True
        await q.edit_message_text("Введи инвайт‑код:", reply_markup=cancel_kb_to_menu())
        return S_JOIN_CODE
    if data == "back:menu":
        await show_main_menu(update, ctx); return S_MENU
    return S_MENU


async def on_group_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); data = q.data
    team_id = ctx.user_data.get("group_id")
    if not team_id:
        await show_main_menu(update, ctx, "Группа не выбрана."); return S_MENU
    conn = db()
    team = conn.execute("SELECT id, name, tz, reminder_time, reminder_days, managers_json, invite_code FROM teams WHERE id=?", (team_id,)).fetchone()
    if not team:
        await q.edit_message_text("Команда не найдена.", reply_markup=team_choice_keyboard(update.effective_user.id)); return S_GROUP_SELECT
    managers = json.loads(team["managers_json"]); is_mgr = update.effective_user.id in managers

    if data == "back:group":
        await q.edit_message_text(f"Команда «{team['name']}» (ID {team_id})", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU

    if data == f"gm:info:{team_id}":
        members = conn.execute("SELECT u.tg_id, u.name FROM team_members tm JOIN users u ON u.tg_id=tm.tg_id WHERE tm.team_id=? ORDER BY u.name COLLATE NOCASE", (team_id,)).fetchall()
        next_run_dt = compute_next_run_local(team["reminder_time"], team["tz"], team["reminder_days"]) if team["reminder_time"] else None
        next_run_label = next_run_dt.strftime("%Y-%m-%d %H:%M") + f" {team['tz']}" if next_run_dt else "—"
        lines = [
            f"Название: {team['name']}",
            f"ID: {team_id}",
            f"Код для вступления: {team['invite_code']}",
            f"TZ: {team['tz']}",
            f"Участников: {len(members)}",
            f"Следующий запуск: {next_run_label}",
            "",
        ]
        for m in members:
            mark = " (менеджер)" if m["tg_id"] in managers else ""
            lines.append(f"• {m['name']}{mark}")
        await q.edit_message_text("\n".join(lines), reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU

    if data == f"gm:view:{team_id}":
        if team["reminder_time"]:
            label = days_to_label(parse_reminder_days(team["reminder_days"]))
            print(f"DEBUG: Viewing schedule - raw_days: {team['reminder_days']}, parsed: {parse_reminder_days(team['reminder_days'])}, label: {label}")
            txt = f"✅ Расписание:\nВремя: {team['reminder_time']}\nTZ: {team['tz']}\nДни: {label}"
        else:
            txt = "Расписание ещё не создано."
        await q.edit_message_text(txt, reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU

    if data == f"gm:edit:{team_id}":
        if not is_mgr:
            await q.edit_message_text("Только менеджер может менять расписание.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU
        ctx.user_data["settime_hhmm"] = None
        await q.edit_message_text("Введите время в формате HH:MM (например, 10:00)", reply_markup=cancel_kb_to_group())
        return S_SET_TIME_HHMM

    if data == f"gm:del:{team_id}":
        if not is_mgr:
            await q.edit_message_text("Только менеджер может удалять расписание.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU
        with conn:
            conn.execute("UPDATE teams SET reminder_time=NULL, reminder_days=NULL WHERE id=?", (team_id,))
        from .jobs import remove_daily_job
        await remove_daily_job(ctx.application, team_id)
        team = conn.execute("SELECT id, name, tz, reminder_time, reminder_days, managers_json, invite_code FROM teams WHERE id=?", (team_id,)).fetchone()
        await q.edit_message_text("✅ Расписание удалено. Дэйлики больше не планируются до создания нового расписания.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU

    if data == f"gm:run:{team_id}":
        if not is_mgr:
            await q.edit_message_text("Только менеджер может запускать дэйлик вручную.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU
        await start_standup(ctx.application, team_id, manual=True)
        await q.edit_message_text("✅ Дэйлик запущен и отправлен всем участникам.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU

    if data == f"gm:members:{team_id}":
        members = conn.execute("SELECT u.tg_id, u.name FROM team_members tm JOIN users u ON u.tg_id=tm.tg_id WHERE tm.team_id=? ORDER BY u.name COLLATE NOCASE", (team_id,)).fetchall()
        names = []
        for m in members:
            mark = " (менеджер)" if m["tg_id"] in managers else ""
            names.append(f"• {m['name']}{mark}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back:group")]])
        await q.edit_message_text("👥 Участники:\n" + ("\n".join(names) if names else "— никого"), reply_markup=kb); return S_GROUP_MENU

    if data == f"gm:leave:{team_id}":
        if is_mgr and len(managers) == 1 and managers[0] == update.effective_user.id:
            await q.edit_message_text("Нельзя выйти: вы единственный менеджер. Назначьте другого менеджера и попробуйте снова.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU
        with conn:
            conn.execute("DELETE FROM team_members WHERE team_id=? AND tg_id=?", (team_id, update.effective_user.id))
            if is_mgr:
                managers = [m for m in managers if m != update.effective_user.id]
                conn.execute("UPDATE teams SET managers_json=? WHERE id=?", (json.dumps(managers), team_id))
        ctx.user_data.pop("group_id", None)
        await q.edit_message_text("Вы вышли из группы.", reply_markup=team_choice_keyboard(update.effective_user.id)); return S_GROUP_SELECT

    if data == f"gm:rmembers:{team_id}":
        if not is_mgr:
            await q.edit_message_text("Только менеджер может удалять участников.", reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU
        members = conn.execute("SELECT u.tg_id, u.name FROM team_members tm JOIN users u ON u.tg_id=tm.tg_id WHERE tm.team_id=? ORDER BY u.name COLLATE NOCASE", (team_id,)).fetchall()
        btns = []
        for m in members:
            if m["tg_id"] == update.effective_user.id:
                continue
            btns.append([InlineKeyboardButton(f"Удалить {m['name']}", callback_data=f"rm:{team_id}:{m['tg_id']}")])
        btns.append([InlineKeyboardButton("◀️ Назад", callback_data="back:group")])
        await q.edit_message_text("Кого удалить?", reply_markup=InlineKeyboardMarkup(btns)); return S_REMOVE_MEMBER_SELECT

    if data == "back:teams":
        await q.edit_message_text("Выберите группу:", reply_markup=team_choice_keyboard(update.effective_user.id)); return S_GROUP_SELECT

    if data == "back:menu":
        await show_main_menu(update, ctx); return S_MENU

    return S_GROUP_MENU


async def on_remove_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); data = q.data
    if data == "back:group":
        return await on_group_menu(update, ctx)
    if not data.startswith("rm:"):
        return S_REMOVE_MEMBER_SELECT
    _, team_id_s, user_id_s = data.split(":")
    team_id = int(team_id_s); user_id = int(user_id_s)
    conn = db()
    team = conn.execute("SELECT managers_json, name FROM teams WHERE id= ?", (team_id,)).fetchone()
    managers = json.loads(team["managers_json"]) if team else []
    if user_id in managers and len(managers) == 1:
        await q.edit_message_text("Нельзя удалить единственного менеджера.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back:group")]])); return S_GROUP_MENU
    with conn:
        conn.execute("DELETE FROM team_members WHERE team_id=? AND tg_id=?", (team_id, user_id))
        if user_id in managers:
            managers = [m for m in managers if m != user_id]
            conn.execute("UPDATE teams SET managers_json=? WHERE id=?", (json.dumps(managers), team_id))
    await q.edit_message_text("Участник удалён.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back:group")]])); return S_GROUP_MENU


async def on_settime_hhmm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hhmm = (update.effective_message.text or "").strip()
    try:
        _ = parse_hhmm(hhmm)
    except Exception:
        await update.effective_message.reply_text("Неверный формат. Пример: 09:30. Введите ещё раз:", reply_markup=cancel_kb_to_group())
        return S_SET_TIME_HHMM
    ctx.user_data["settime_hhmm"] = hhmm
    await update.effective_message.reply_text("Выбери смещение часового пояса (UTC±N):", reply_markup=tz_offset_keyboard())
    return S_SET_TIME_TZ


async def on_tz_offset_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); data = q.data
    if data.startswith("tzo:"):
        off = int(data.split(":", 1)[1])
        tz_name = f"UTC{off:+d}"
        ctx.user_data["settime_tz"] = tz_name
        await q.edit_message_text(f"Часовой пояс: {tz_name}. Выберите расписание:", reply_markup=schedule_preset_keyboard()); return S_SET_SCHEDULE
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
    q = update.callback_query; await q.answer(); data = q.data
    team_id = ctx.user_data.get("group_id")
    if not team_id:
        await show_main_menu(update, ctx, "Группа не выбрана."); return S_MENU

    from .utils import parse_reminder_days

    def finish_save(days: tuple[int, ...]):
        hhmm = ctx.user_data.get("settime_hhmm")
        tz_name = ctx.user_data.get("settime_tz")
        uid = update.effective_user.id
        if not hhmm or not tz_name:
            return "Не хватает данных. Начните заново."
        conn = db()
        team = conn.execute("SELECT name, managers_json FROM teams WHERE id=?", (team_id,)).fetchone()
        if not team:
            return "Команда не найдена."
        if uid not in json.loads(team["managers_json"]):
            return "Только менеджер может менять расписание."
        days_json = json.dumps(list(days))
        print(f"DEBUG: Saving schedule - days: {days}, days_json: {days_json}, label: {days_to_label(days)}")
        with conn:
            conn.execute("UPDATE teams SET reminder_time=?, tz=?, reminder_days=? WHERE id=?", (hhmm, tz_name, days_json, team_id))
        for k in ("settime_hhmm","settime_tz","settime_days"):
            ctx.user_data.pop(k, None)
        asyncio.create_task(reschedule_daily_job(ctx.application, team_id))
        return f"Ок! Время дэйлика: {hhmm} ({tz_name}), дни: {days_to_label(days)}."

    if data == "sch:preset:everyday":
        msg = finish_save(tuple(range(7)))
    elif data == "sch:preset:weekdays":
        msg = finish_save(tuple(range(5)))
    elif data == "sch:preset:weekends":
        msg = finish_save((5, 6))
    elif data.startswith("sch:custom"):
        sel = set(ctx.user_data.get("settime_days", set()))
        if data == "sch:custom:start":
            if not sel: sel = set(range(5))
            ctx.user_data["settime_days"] = sel
            await q.edit_message_text("Отметьте дни недели:", reply_markup=schedule_custom_keyboard(sel)); return S_SET_SCHEDULE
        if data == "sch:custom:reset":
            ctx.user_data["settime_days"] = set()
            await q.edit_message_text("Отметьте дни недели:", reply_markup=schedule_custom_keyboard(set())); return S_SET_SCHEDULE
        if data == "sch:custom:save":
            days = tuple(sorted(ctx.user_data.get("settime_days", set())))
            if not days:
                await q.edit_message_text("Нужно выбрать хотя бы один день.", reply_markup=schedule_custom_keyboard(set())); return S_SET_SCHEDULE
            msg = finish_save(days)
        if data.startswith("sch:custom:toggle:"):
            d = int(data.rsplit(":", 1)[1])
            if d in sel: sel.remove(d)
            else: sel.add(d)
            ctx.user_data["settime_days"] = sel
            await q.edit_message_text("Отметьте дни недели:", reply_markup=schedule_custom_keyboard(sel)); return S_SET_SCHEDULE
    else:
        await q.edit_message_text("Выберите расписание:", reply_markup=schedule_preset_keyboard()); return S_SET_SCHEDULE

    conn = db()
    team = conn.execute("SELECT id, name, tz, reminder_time, reminder_days, managers_json, invite_code FROM teams WHERE id=?", (team_id,)).fetchone()
    is_mgr = update.effective_user.id in json.loads(team["managers_json"])
    await q.edit_message_text(msg, reply_markup=group_menu_keyboard(team, is_mgr, update.effective_user.id)); return S_GROUP_MENU


async def on_text_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("await_create_team_name"):
        name = (update.effective_message.text or "").strip()
        code = gen_invite_code()
        manager_id = update.effective_user.id
        conn = db()
        with conn:
            cur = conn.execute("INSERT INTO teams (name, invite_code, tz, reminder_time, reminder_days, managers_json) VALUES (?, ?, 'UTC', NULL, NULL, ?)", (name, code, json.dumps([manager_id])))
            team_id = cur.lastrowid
            conn.execute("INSERT OR IGNORE INTO team_members (team_id, tg_id) VALUES (?, ?)", (team_id, manager_id))
        ctx.user_data.pop("await_create_team_name", None)
        await update.effective_message.reply_text(
            f"Команда создана!\nID: {team_id}\nКод: {code}\nТеперь выбери группу, чтобы перейти в её настройки.",
            reply_markup=team_choice_keyboard(update.effective_user.id)
        )
        return S_GROUP_SELECT

    if ctx.user_data.get("await_join_code"):
        code = (update.effective_message.text or "").strip().upper()
        conn = db()
        team = conn.execute("SELECT id, name FROM teams WHERE invite_code=?", (code,)).fetchone()
        if not team:
            await update.effective_message.reply_text("Неверный код. Попробуйте снова.", reply_markup=cancel_kb_to_menu()); return S_JOIN_CODE
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
                if not team: continue
                today = today_in_tz(team["tz"]).isoformat()
                st = conn.execute("SELECT id FROM standups WHERE team_id=? AND date_iso=? ORDER BY id DESC LIMIT 1", (team_id, today)).fetchone()
                if not st: continue
                upd = conn.execute("SELECT id, answered FROM updates WHERE standup_id=? AND tg_id=?", (st["id"], uid)).fetchone()
                if not upd or upd["answered"] == 1: continue
                with conn:
                    conn.execute("UPDATE updates SET text=?, created_utc=?, answered=1 WHERE id=?", (text.strip(), now_utc().isoformat(), upd["id"]))
                updated_any = True
            await msg.reply_text("Принято. Спасибо!" if updated_any else "Ответ сохранён или активных дэйликов нет.")
        return ConversationHandler.END

    return ConversationHandler.END


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


