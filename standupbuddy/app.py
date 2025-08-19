from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters
)

from .config import BOT_TOKEN
from .handlers import (
    cmd_start, cmd_help, cmd_health,
    on_menu_click, on_group_menu, on_settime_hhmm, on_tz_offset_pick,
    on_settime_tz_manual, on_schedule_pick, on_remove_member, on_text_flow,
    on_error,
)
from .states import (
    S_MENU, S_GROUP_SELECT, S_GROUP_MENU, S_CREATE_TEAM_NAME, S_JOIN_CODE,
    S_SET_TIME_HHMM, S_SET_TIME_TZ, S_SET_SCHEDULE, S_REMOVE_MEMBER_SELECT,
)


def build_app() -> Application:
    app: Application = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("help", cmd_help),
            CommandHandler("health", cmd_health),
        ],
        states={
            S_MENU: [CallbackQueryHandler(on_menu_click)],
            S_GROUP_SELECT: [CallbackQueryHandler(on_menu_click)],
            S_GROUP_MENU: [CallbackQueryHandler(on_group_menu)],
            S_CREATE_TEAM_NAME: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_flow),
                CallbackQueryHandler(on_menu_click),
            ],
            S_JOIN_CODE: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_flow),
                CallbackQueryHandler(on_menu_click),
            ],
            S_SET_TIME_HHMM: [
                MessageHandler(filters.TEXT & (~filters.COMMAND), on_settime_hhmm),
                CallbackQueryHandler(on_group_menu),
            ],
            S_SET_TIME_TZ: [
                CallbackQueryHandler(on_tz_offset_pick),
                MessageHandler(filters.TEXT & (~filters.COMMAND), on_settime_tz_manual),
            ],
            S_SET_SCHEDULE: [
                CallbackQueryHandler(on_schedule_pick),
            ],
            S_REMOVE_MEMBER_SELECT: [
                CallbackQueryHandler(on_remove_member)
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CommandHandler("help", cmd_help),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text_flow))
    app.add_error_handler(on_error)

    return app


