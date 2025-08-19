import json
import random
import string
from datetime import datetime, timezone, time, timedelta

import pytz
from telegram import Update


def gen_invite_code(n: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def tz_from_str(tz_str: str):
    if tz_str and tz_str.upper().startswith("UTC"):
        rest = tz_str[3:]
        sign = 1
        if rest.startswith("+"):
            rest = rest[1:]
            sign = 1
        elif rest.startswith("-"):
            rest = rest[1:]
            sign = -1
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


def get_user_name(u: Update) -> str:
    user = u.effective_user
    if not user:
        return "Unknown"
    full = " ".join(x for x in [user.first_name, user.last_name] if x)
    return full or (user.username or str(user.id))


def parse_reminder_days(raw: str | None):
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


def days_to_label(days):
    days = sorted(set(int(d) for d in days))
    if tuple(days) == tuple(range(7)):
        return "каждый день"
    if tuple(days) == tuple(range(5)):
        return "по будням"
    if tuple(days) == (5, 6):
        return "по выходным"
    names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    return ", ".join(names[d] for d in days)


def compute_next_run_local(reminder_time_str: str | None, tz_name: str, reminder_days_raw: str | None):
    if not reminder_time_str:
        return None
    days = parse_reminder_days(reminder_days_raw)
    if not days:
        days = tuple(range(7))
    tz = tz_from_str(tz_name)
    now = datetime.now(tz)
    hhmm = parse_hhmm(reminder_time_str)
    today_candidate = datetime(now.year, now.month, now.day, hhmm.hour, hhmm.minute, tzinfo=tz)
    if now <= today_candidate and now.weekday() in days:
        return today_candidate
    for add_days in range(1, 15):
        d = now + timedelta(days=add_days)
        if d.weekday() in days:
            return datetime(d.year, d.month, d.day, hhmm.hour, hhmm.minute, tzinfo=tz)
    return None


