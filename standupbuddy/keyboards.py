from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from .db import db


def main_menu(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать команду", callback_data="m:create")],
        [InlineKeyboardButton("🔗 Вступить по коду", callback_data="m:join")],
        [InlineKeyboardButton("👥 Мои команды", callback_data="m:teams")],
    ])


def group_menu_keyboard(team_row, is_manager: bool, self_id: int) -> InlineKeyboardMarkup:
    has_schedule = bool(team_row["reminder_time"])
    btns = []
    if has_schedule:
        btns.append([InlineKeyboardButton("📄 Посмотреть расписание", callback_data=f"gm:view:{team_row['id']}")])
        if is_manager:
            btns.append([InlineKeyboardButton("✏️ Редактировать расписание", callback_data=f"gm:edit:{team_row['id']}")])
            btns.append([InlineKeyboardButton("🗑 Удалить расписание", callback_data=f"gm:del:{team_row['id']}")])
            btns.append([InlineKeyboardButton("▶️ Запустить сейчас", callback_data=f"gm:run:{team_row['id']}")])
    else:
        if is_manager:
            btns.append([InlineKeyboardButton("➕ Создать расписание", callback_data=f"gm:edit:{team_row['id']}")])
    btns.append([InlineKeyboardButton("👥 Участники", callback_data=f"gm:members:{team_row['id']}")])
    btns.append([InlineKeyboardButton("ℹ️ Инфо о группе", callback_data=f"gm:info:{team_row['id']}")])
    btns.append([InlineKeyboardButton("↩️ Выйти из группы", callback_data=f"gm:leave:{team_row['id']}")])
    if is_manager:
        btns.append([InlineKeyboardButton("❌ Удалить участника…", callback_data=f"gm:rmembers:{team_row['id']}")])
    btns.append([InlineKeyboardButton("◀️ К списку групп", callback_data="back:teams")])
    btns.append([InlineKeyboardButton("🏠 В меню", callback_data="back:menu")])
    return InlineKeyboardMarkup(btns)


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
    rows, row = [], []
    for off in range(-12, 15):
        row.append(InlineKeyboardButton(f"UTC{off:+d}", callback_data=f"tzo:{off}"))
        if len(row) == 3:
            rows.append(row); row = []
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
    rows.append([InlineKeyboardButton("Сохранить", callback_data="sch:custom:save"),
                 InlineKeyboardButton("Сброс", callback_data="sch:custom:reset")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back:schedule")])
    return InlineKeyboardMarkup(rows)


