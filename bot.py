# Чтобы планировщик PTB (APScheduler) не падал на Windows из-за timezone (pytz vs zoneinfo)
import pytz
try:
    import apscheduler.util as _aps_util
    _orig_astimezone = getattr(_aps_util, "astimezone", None)
    if _orig_astimezone:

        def _astimezone(val):
            if val is None:
                return pytz.timezone("Europe/Moscow")
            if isinstance(val, pytz.BaseTzInfo):
                return val
            try:
                if hasattr(val, "zone"):
                    return pytz.timezone(val.zone)
                if hasattr(val, "key") and getattr(val, "key", None):
                    return pytz.timezone(val.key)
            except Exception:
                pass
            return pytz.timezone("Europe/Moscow")

        _aps_util.astimezone = _astimezone
except Exception:
    pass

import asyncio
import json
import os
from dataclasses import dataclass, asdict
from urllib.parse import quote
from datetime import date, datetime, time, timedelta
import calendar
from typing import Dict, List, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BOT_TOKEN = os.getenv("BOT_TOKEN")

# Куда пересылать отзывы и новые записи: аккаунт https://t.me/exe_141592
# ID сохраняется автоматически, когда этот пользователь нажимает /start в боте
ADMIN_USERNAME = "exe_141592"

# Папка для данных (на Amvera задайте DATA_DIR=/data, локально — по умолчанию текущая)
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)) or ".")
ADMIN_ID_FILE = os.path.join(DATA_DIR, "admin_id.txt")
CHANNEL_URL = "https://t.me/prog_Dinis"

# Хранение данных: JSON-файл — просто и достаточно для одного салона.
DATA_FILE = os.path.join(DATA_DIR, "bookings.json")


STATE_WAITING_NAME = "waiting_name"
STATE_WAITING_PHONE = "waiting_phone"
STATE_MAIN_MENU = "main_menu"
STATE_SELECTING_SERVICES = "selecting_services"
STATE_SELECTING_DATE = "selecting_date"
STATE_SELECTING_TIME = "selecting_time"
STATE_SELECTING_MASTER = "selecting_master"
STATE_WAITING_REVIEW = "waiting_review"


MASTERS = ["Марина", "Оля", "Анна", "Полина"]
TIME_SLOTS = ["10:00", "12:00", "14:00", "16:00", "18:00"]


@dataclass
class Service:
    id: str
    name: str
    price: int
    duration_minutes: int


SERVICES: List[Service] = [
    Service("combo_manicure", "Маникюр комбинированный", 1500, 40),
    Service("gel_polish", "Маникюр с покрытием гель-лака", 2200, 90),
    Service("nail_extension", "Наращивание ногтей (длина 1–6)", 3000, 120),
    Service("design", "Дизайн", 500, 30),
]

SERVICES_BY_ID: Dict[str, Service] = {s.id: s for s in SERVICES}


def get_admin_chat_id() -> Optional[int]:
    """ID чата администратора (сохраняется при /start от @exe_141592)."""
    if not os.path.exists(ADMIN_ID_FILE):
        return None
    try:
        with open(ADMIN_ID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def save_admin_id(user_id: int) -> None:
    try:
        with open(ADMIN_ID_FILE, "w", encoding="utf-8") as f:
            f.write(str(user_id))
    except Exception:
        pass


async def _notify_manager_new_booking(
    context: ContextTypes.DEFAULT_TYPE,
    booking: Dict,
    services_text: str,
    total_price: int,
) -> None:
    """Отправить менеджеру уведомление о новой записи в удобном виде."""
    admin_chat_id = get_admin_chat_id()
    if not admin_chat_id:
        return
    date_str = booking.get("date", "")
    try:
        d = date.fromisoformat(date_str)
        date_display = d.strftime("%d.%m.%Y")
    except Exception:
        date_display = date_str
    time_display = _format_time_display(booking.get("time") or "")
    master_display = _format_master_display(booking.get("master") or "")
    text = (
        "📋 Новая запись\n\n"
        f"👤 Клиент: {booking.get('client_name') or booking.get('user_name') or '—'}\n"
        f"📞 Телефон: {booking.get('phone') or '—'}\n\n"
        f"Услуги:\n{services_text}\n\n"
        f"📅 Дата и время: {date_display}, {time_display}\n"
        f"👩 Мастер: {master_display}\n"
        f"💰 Сумма: {total_price} ₽\n\n"
        f"ID записи: {booking.get('id')}"
    )
    try:
        await context.bot.send_message(chat_id=admin_chat_id, text=text)
    except Exception:
        pass


class BookingStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.bookings: List[Dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.bookings = json.load(f)
            except Exception:
                self.bookings = []
        else:
            self.bookings = []

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.bookings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add_booking(self, booking: Dict) -> None:
        self.bookings.append(booking)
        self._save()

    def remove_booking(self, booking_id: str, user_id: int) -> bool:
        for i, b in enumerate(self.bookings):
            if b.get("id") == booking_id and b.get("user_id") == user_id:
                del self.bookings[i]
                self._save()
                return True
        return False

    def get_user_bookings(self, user_id: int) -> List[Dict]:
        return [b for b in self.bookings if b.get("user_id") == user_id]

    def get_bookings_next_7_days(self) -> List[Dict]:
        today = date.today()
        end = today + timedelta(days=6)
        result = []
        for b in self.bookings:
            d = b.get("date")
            if not d:
                continue
            try:
                bd = date.fromisoformat(d)
                if today <= bd <= end:
                    result.append(b)
            except (ValueError, TypeError):
                continue
        return sorted(result, key=lambda x: (x.get("date", ""), x.get("time", "")))

    def is_slot_taken(self, day: date, time_str: str, master: str) -> bool:
        time_norm = _format_time_display(time_str)
        for b in self.bookings:
            if (
                b.get("date") == day.isoformat()
                and _format_time_display(b.get("time") or "") == time_norm
                and b.get("master") == master
            ):
                return True
        return False

    def has_free_master(self, day: date, time_str: str) -> bool:
        for m in MASTERS:
            if not self.is_slot_taken(day, time_str, m):
                return True
        return False

    def is_day_fully_booked(self, day: date) -> bool:
        for t in TIME_SLOTS:
            if self.has_free_master(day, t):
                return False
        return True


booking_store = BookingStore(DATA_FILE)


def _is_admin(user) -> bool:
    if not user or not getattr(user, "username", None):
        return False
    return user.username.lower() == ADMIN_USERNAME.lower()


def get_main_menu_keyboard(user=None) -> ReplyKeyboardMarkup:
    keyboard = [
        ["Записаться на услугу", "Мои записи"],
        ["О нас", "Адрес"],
        ["Отзывы", "Наш телеграмм канал"],
    ]
    if user and _is_admin(user):
        keyboard.append(["📋 Все записи"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_contact_keyboard() -> ReplyKeyboardMarkup:
    button = KeyboardButton("Отправить номер телефона", request_contact=True)
    keyboard = [[button]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def get_services_keyboard(selected_ids: Optional[List[str]] = None) -> InlineKeyboardMarkup:
    if selected_ids is None:
        selected_ids = []
    keyboard: List[List[InlineKeyboardButton]] = []
    for s in SERVICES:
        mark = "✅" if s.id in selected_ids else "➕"
        text = f"{mark} {s.name} - {s.price} ₽ - {s.duration_minutes} мин"
        keyboard.append(
            [InlineKeyboardButton(text, callback_data=f"svc_toggle:{s.id}")]
        )
    keyboard.append(
        [
            InlineKeyboardButton("Готово", callback_data="svc_done"),
            InlineKeyboardButton("Меню", callback_data="back_to_menu"),
        ]
    )
    return InlineKeyboardMarkup(keyboard)


MONTHS_RU = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}


def build_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    today = date.today()
    keyboard: List[List[InlineKeyboardButton]] = []
    title = f"{MONTHS_RU.get(month, month)} {year}"
    keyboard.append([InlineKeyboardButton(title, callback_data="ignore")])

    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append(
        [InlineKeyboardButton(d, callback_data="ignore") for d in week_days]
    )

    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdatescalendar(year, month):
        row: List[InlineKeyboardButton] = []
        for day in week:
            if day.month != month:
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))
                continue
            if day < today:
                row.append(
                    InlineKeyboardButton(str(day.day), callback_data="ignore")
                )
                continue
            if booking_store.is_day_fully_booked(day):
                text = "•"
            else:
                text = str(day.day)
            row.append(
                InlineKeyboardButton(
                    text, callback_data=f"cal_day:{year}:{month}:{day.day}"
                )
            )
        keyboard.append(row)

    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_month = month + 1
    next_year = year
    if next_month == 13:
        next_month = 1
        next_year += 1

    keyboard.append(
        [
            InlineKeyboardButton(
                "«", callback_data=f"cal_nav:{prev_year}:{prev_month}"
            ),
            InlineKeyboardButton("Отмена", callback_data="cal_cancel"),
            InlineKeyboardButton(
                "»", callback_data=f"cal_nav:{next_year}:{next_month}"
            ),
        ]
    )

    return InlineKeyboardMarkup(keyboard)


def _format_master_display(m: str) -> str:
    """Убрать ошибочный префикс 00: у имени мастера (из старых записей)."""
    if not m:
        return ""
    s = str(m).strip()
    if s.startswith("00:"):
        return s[3:].strip() or s
    return s


def _format_time_display(t: str) -> str:
    """Всегда показывать время в виде 14:00, а не 14 или 16."""
    if t is None:
        return ""
    s = str(t).strip()
    if not s:
        return ""
    if ":" in s:
        parts = s.split(":", 1)
        try:
            h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            return f"{h:02d}:{m:02d}"
        except (ValueError, TypeError):
            return s
    try:
        h = int(s)
        return f"{h:02d}:00"
    except (ValueError, TypeError):
        return s


def _parse_slot(s: str) -> time:
    """Парсит "14:00" или "14" в time."""
    if not s:
        return time(0, 0)
    s = str(s).strip()
    if ":" in s:
        parts = s.split(":", 1)
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return time(h, m)
    return time(int(s), 0)


def get_time_keyboard(day: date) -> InlineKeyboardMarkup:
    today = date.today()
    now = datetime.now()
    buttons: List[List[InlineKeyboardButton]] = []
    for t in TIME_SLOTS:
        if day < today:
            continue
        if day == today and _parse_slot(t) < now.time():
            continue
        if booking_store.has_free_master(day, t):
            buttons.append(
                [InlineKeyboardButton(t, callback_data=f"time:{day.isoformat()}:{t}")]
            )
    buttons.append([InlineKeyboardButton("Назад к календарю", callback_data="back_to_calendar")])
    return InlineKeyboardMarkup(buttons)


def get_masters_keyboard(day: date, time_str: str) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    for m in MASTERS:
        if not booking_store.is_slot_taken(day, time_str, m):
            buttons.append(
                [
                    InlineKeyboardButton(
                        m,
                        callback_data=f"master:{day.isoformat()}:{time_str}:{m}",
                    )
                ]
            )
    buttons.append([InlineKeyboardButton("Отмена", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.username and user.username.lower() == ADMIN_USERNAME.lower():
        save_admin_id(user.id)
    context.user_data.clear()
    context.user_data["state"] = STATE_WAITING_NAME

    channel_url = CHANNEL_URL
    text = (
        "Привет! 👋\n\n"
        "Я бот для записи на маникюр. 💅\n\n"
        f"📢 Наш Telegram-канал: {channel_url}\n\n"
        "✍️ Пожалуйста, напишите ваше имя и фамилию одним сообщением.(это нужно для записи на наши услуги)\n\n"
        "Например: Настя Иванова"
    )

    await update.message.reply_text(text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")

    if state == STATE_WAITING_NAME:
        await handle_name(update, context, text)
        return

    if state == STATE_WAITING_PHONE:
        await update.message.reply_text(
            "📱 Пожалуйста, отправьте номер телефона кнопкой ниже.", reply_markup=get_contact_keyboard()
        )
        return

    if state == STATE_WAITING_REVIEW:
        await handle_review_message(update, context, text)
        return

    # Основное меню
    lowered = text.lower()
    if lowered == "записаться на услугу":
        await start_booking(update, context)
    elif lowered == "мои записи":
        await show_my_bookings(update, context)
    elif lowered == "отзывы":
        await show_reviews_info(update, context)
    elif lowered == "наш телеграмм канал":
        await show_channel_link(update, context)
    elif lowered == "о нас":
        await show_about_us(update, context)
    elif lowered == "адрес":
        await show_address(update, context)
    elif "все записи" in lowered:
        await show_all_bookings_for_admin(update, context)
    else:
        await update.message.reply_text(
            "👆 Пожалуйста, выберите одну из кнопок меню ниже.",
            reply_markup=get_main_menu_keyboard(update.effective_user),
        )


async def handle_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE, name_text: str
) -> None:
    context.user_data["profile"] = {"full_name": name_text}
    context.user_data["state"] = STATE_WAITING_PHONE

    await update.message.reply_text(
        "📱 Теперь отправьте номер телефона кнопкой ниже — это нужно для записи.",
        reply_markup=get_contact_keyboard(),
    )


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact = update.message.contact
    if not contact:
        return

    profile = context.user_data.get("profile", {})
    profile["phone"] = contact.phone_number
    context.user_data["profile"] = profile
    context.user_data["state"] = STATE_MAIN_MENU

    await update.message.reply_text(
        "✅ Спасибо! Мы записали ваши данные.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "👇 Выберите кнопку ниже.",
        reply_markup=get_main_menu_keyboard(update.effective_user),
    )


async def start_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["state"] = STATE_SELECTING_SERVICES
    context.user_data["current_booking"] = {
        "services": [],
        "date": None,
        "time": None,
        "master": None,
    }

    if update.message:
        await update.message.reply_text(
            "💅 Выберите одну или несколько услуг:",
            reply_markup=get_services_keyboard([]),
        )
    else:
        await update.callback_query.message.reply_text(
            "💅 Выберите одну или несколько услуг:",
            reply_markup=get_services_keyboard([]),
        )


def summarize_services(service_ids: List[str]) -> (str, int, int):
    lines: List[str] = []
    total_price = 0
    total_duration = 0
    for sid in service_ids:
        s = SERVICES_BY_ID.get(sid)
        if not s:
            continue
        lines.append(f"- {s.name} — {s.price} ₽, {s.duration_minutes} мин")
        total_price += s.price
        total_duration += s.duration_minutes
    summary_text = "\n".join(lines) if lines else "Услуги не выбраны."
    return summary_text, total_price, total_duration


async def show_booking_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = context.user_data.get("current_booking", {})
    service_ids = current.get("services", [])
    services_text, total_price, total_duration = summarize_services(service_ids)

    text = (
        "💅 Вы выбрали услугу(и):\n"
        f"{services_text}\n\n"
        f"💰 Итоговая цена: {total_price} ₽\n"
        f"⏱ Общее время: {total_duration} мин"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Добавить или убрать услугу", callback_data="edit_services")],
            [InlineKeyboardButton("📅 Выбрать день", callback_data="choose_day")],
            [InlineKeyboardButton("📋 Мои записи", callback_data="my_bookings")],
            [InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu")],
        ]
    )

    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def show_my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    bookings = booking_store.get_user_bookings(user.id)

    if not bookings:
        text = "📋 У вас пока нет записей."
        if update.callback_query:
            await update.callback_query.message.reply_text(
                text, reply_markup=get_main_menu_keyboard(update.effective_user)
            )
        else:
            await update.message.reply_text(text, reply_markup=get_main_menu_keyboard(update.effective_user))
        return

    parts: List[str] = ["📋 Ваши записи:"]
    buttons: List[List[InlineKeyboardButton]] = []
    for b in bookings:
        services_text, total_price, total_duration = summarize_services(b.get("services", []))
        date_str = b.get("date", "")
        try:
            d = date.fromisoformat(date_str)
            date_short = d.strftime("%d.%m")
        except Exception:
            date_short = date_str[:10] if len(date_str) >= 10 else date_str
        time_short = _format_time_display(b.get("time") or "")
        master_short = _format_master_display(b.get("master") or "")
        btn_label = f"❌ {date_short} {time_short} · {master_short}"
        booking_id = b.get("id", "")
        if booking_id:
            buttons.append([
                InlineKeyboardButton(btn_label, callback_data=f"cancel_booking:{booking_id}")
            ])
        parts.append(
            f"\n📅 {date_short} в {time_short} · {master_short}\n"
            f"Услуги:\n{services_text}\n"
            f"💰 {total_price} ₽ (≈ {total_duration} мин)"
        )

    text = "\n".join(parts)

    buttons.append([InlineKeyboardButton("🏠 Меню", callback_data="back_to_menu")])
    keyboard = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def show_all_bookings_for_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать все записи на 7 дней — только для @exe_141592."""
    if not _is_admin(update.effective_user):
        msg = "👆 Пожалуйста, выберите одну из кнопок меню ниже."
        kb = get_main_menu_keyboard(update.effective_user)
        if update.message:
            await update.message.reply_text(msg, reply_markup=kb)
        else:
            await update.callback_query.message.reply_text(msg, reply_markup=kb)
        return
    bookings = booking_store.get_bookings_next_7_days()
    today = date.today()
    end = today + timedelta(days=6)
    head = f"📋 Все записи на {today.strftime('%d.%m')} – {end.strftime('%d.%m.%Y')}\n"
    if not bookings:
        text = head + "\nНет записей."
    else:
        parts = [head]
        for b in bookings:
            services_text, total_price, _ = summarize_services(b.get("services", []))
            date_str = b.get("date", "")
            try:
                d = date.fromisoformat(date_str)
                date_display = d.strftime("%d.%m.%Y")
            except Exception:
                date_display = date_str
            time_display = _format_time_display(b.get("time") or "")
            master_display = _format_master_display(b.get("master") or "")
            client = b.get("client_name") or b.get("user_name") or "—"
            phone = b.get("phone") or "—"
            parts.append(
                f"\n📅 {date_display} {time_display}\n"
                f"👤 {client}\n📞 {phone}\n👩 {master_display}\n"
                f"Услуги: {services_text.replace(chr(10), ', ')}\n💰 {total_price} ₽"
            )
        text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:3990] + "\n\n… (сообщение обрезано)"
    if update.message:
        await update.message.reply_text(text, reply_markup=get_main_menu_keyboard(update.effective_user))
    else:
        await update.callback_query.message.reply_text(
            text, reply_markup=get_main_menu_keyboard(update.effective_user)
        )


REVIEWS_CHANNEL_URL = "https://t.me/+TUSsrRxshz1mNjVi"


async def show_reviews_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "💬 Отзывы\n\n"
        "Вы можете написать свой отзыв — он придёт администратору. "
        "Или посмотреть отзывы других клиентов в нашем канале."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✍️ Написать отзыв", callback_data="write_review")],
            [InlineKeyboardButton("👀 Смотреть отзывы", url=REVIEWS_CHANNEL_URL)],
        ]
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def start_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["state"] = STATE_WAITING_REVIEW
    await update.callback_query.message.reply_text(
        "✍️ Напишите ваш отзыв одним сообщением."
    )


async def handle_review_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    context.user_data["state"] = STATE_MAIN_MENU
    user = update.effective_user
    profile = context.user_data.get("profile", {})
    name = profile.get("full_name", user.full_name or "Не указано")

    admin_chat_id = get_admin_chat_id()
    sent = False
    if admin_chat_id:
        try:
            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=(
                    "📩 Новый отзыв\n\n"
                    f"От: {name}\n"
                    f"Username: @{user.username or '—'}\n"
                    f"ID: {user.id}\n\n"
                    f"Текст:\n{text}"
                ),
            )
            sent = True
        except Exception:
            pass

    if sent:
        await update.message.reply_text(
            "✅ Спасибо! Ваш отзыв отправлен администратору.",
            reply_markup=get_main_menu_keyboard(update.effective_user),
        )
    else:
        await update.message.reply_text(
            "✅ Спасибо! Ваш отзыв принят. Чтобы получать отзывы в Telegram, "
            "один раз нажмите /start в этом боте с аккаунта @exe_141592.",
            reply_markup=get_main_menu_keyboard(update.effective_user),
        )


async def show_channel_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📢 Открыть канал", url=CHANNEL_URL)]]
    )
    text = "📢 Наш Telegram-канал. Будем рады вашей подписке!"
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


ABOUT_US_TEXT = (
    "✨ ProNail — пространство безупречного маникюра и уюта.\n\n"
    "Мы создаем стильный и аккуратный дизайн, который будет радовать вас 3–4 недели. "
    "В работе используем только профессиональные материалы и стерильные инструменты. "
    "Наши мастера — это команда с опытом от 5 лет, которая знает всё о здоровье и красоте ваших ногтей.\n\n"
    "К нам приходят, чтобы отдохнуть за чашечкой ароматного кофе и получить качественный сервис без спешки.\n\n"
    "📍 Ждем вас по адресу: просп. Пацаева, 7, корп. 1\n"
    "📞 Телефон: 89857972220\n"
    f"📱 Телеграмм канал: {CHANNEL_URL}"
)


async def show_about_us(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Записаться", callback_data="book_service")],
            [InlineKeyboardButton("💬 Отзывы", callback_data="reviews_page")],
        ]
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(
            ABOUT_US_TEXT, reply_markup=keyboard
        )
    else:
        await update.message.reply_text(ABOUT_US_TEXT, reply_markup=keyboard)


SALON_ADDRESS = "просп. Пацаева, 7, корп. 1"
SALON_PHONE = "89857972220"
YANDEX_MAPS_URL = "https://yandex.ru/maps/?"


async def show_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    maps_url = YANDEX_MAPS_URL + "text=" + quote(SALON_ADDRESS)
    text = (
        f"📍 Адрес: {SALON_ADDRESS}\n\n"
        f"📞 Телефон: {SALON_PHONE}"
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗺 Открыть на карте (Яндекс.Карты)", url=maps_url)]]
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def handle_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "ignore":
        return

    if data == "back_to_menu":
        context.user_data["state"] = STATE_MAIN_MENU
        await query.message.reply_text(
            "👇 Выберите кнопку ниже.", reply_markup=get_main_menu_keyboard(update.effective_user)
        )
        return

    if data == "my_bookings":
        await show_my_bookings(update, context)
        return

    if data == "write_review":
        await start_review(update, context)
        return

    if data == "book_service":
        await start_booking(update, context)
        return

    if data == "reviews_page":
        await show_reviews_info(update, context)
        return

    if data.startswith("cancel_booking:"):
        booking_id = data.split(":", 1)[1]
        user_id = update.effective_user.id
        success = await asyncio.to_thread(booking_store.remove_booking, booking_id, user_id)
        if success:
            await query.message.reply_text(
                "✅ Запись отменена.",
                reply_markup=get_main_menu_keyboard(update.effective_user),
            )
        else:
            await query.message.reply_text(
                "❌ Не удалось отменить запись.",
                reply_markup=get_main_menu_keyboard(update.effective_user),
            )
        return

    if data.startswith("svc_toggle:"):
        service_id = data.split(":", maxsplit=1)[1]
        current = context.user_data.get("current_booking", {})
        services = current.get("services", [])
        if service_id in services:
            services.remove(service_id)
        else:
            services.append(service_id)
        current["services"] = services
        context.user_data["current_booking"] = current

        await query.message.edit_reply_markup(
            reply_markup=get_services_keyboard(services)
        )
        return

    if data == "svc_done":
        current = context.user_data.get("current_booking", {})
        services = current.get("services", [])
        if not services:
            await query.message.reply_text(
                "💅 Пожалуйста, выберите хотя бы одну услугу."
            )
            return
        await show_booking_summary(update, context)
        return

    if data == "edit_services":
        current = context.user_data.get("current_booking", {})
        services = current.get("services", [])
        await query.message.reply_text(
            "✏️ Обновите список услуг:",
            reply_markup=get_services_keyboard(services),
        )
        return

    if data == "choose_day":
        today = date.today()
        context.user_data["state"] = STATE_SELECTING_DATE
        await query.message.reply_text(
            "📅 Выберите день:", reply_markup=build_calendar(today.year, today.month)
        )
        return

    if data.startswith("cal_nav:"):
        _, y, m = data.split(":")
        year = int(y)
        month = int(m)
        await query.message.edit_reply_markup(
            reply_markup=build_calendar(year, month)
        )
        return

    if data == "cal_cancel":
        context.user_data["state"] = STATE_MAIN_MENU
        await query.message.reply_text(
            "❌ Выбор даты отменён.", reply_markup=get_main_menu_keyboard(update.effective_user)
        )
        return

    if data.startswith("cal_day:"):
        _, y, m, d = data.split(":")
        selected_date = date(int(y), int(m), int(d))
        today = date.today()
        if selected_date < today:
            await query.message.reply_text(
                "⚠️ Нельзя записаться на прошедшую дату. Выберите сегодня или другой день."
            )
            return
        if booking_store.is_day_fully_booked(selected_date):
            await query.message.reply_text(
                "😔 К сожалению, в этот день нет свободного времени. Выберите другой день."
            )
            return

        time_kb = get_time_keyboard(selected_date)
        current = context.user_data.get("current_booking", {})
        current["date"] = selected_date.isoformat()
        context.user_data["current_booking"] = current
        context.user_data["state"] = STATE_SELECTING_TIME

        text = f"📅 Вы выбрали дату: {selected_date.strftime('%d.%m.%Y')}\n\n🕐 Выберите время:"
        if selected_date == today and len(time_kb.inline_keyboard) == 1:
            text = (
                "⏰ На сегодня свободное время уже прошло. Выберите другой день."
            )
        await query.message.reply_text(text, reply_markup=time_kb)
        return

    if data == "back_to_calendar":
        current = context.user_data.get("current_booking", {})
        date_str = current.get("date")
        if date_str:
            dt = datetime.fromisoformat(date_str).date()
        else:
            dt = date.today()
        await query.message.reply_text(
            "📅 Выберите день:", reply_markup=build_calendar(dt.year, dt.month)
        )
        return

    if data.startswith("time:"):
        _, day_str, time_str = data.split(":", 2)
        selected_date = date.fromisoformat(day_str)
        today = date.today()
        if selected_date < today:
            await query.message.reply_text(
                "⚠️ Нельзя записаться на прошедшую дату. Выберите другой день.",
                reply_markup=get_main_menu_keyboard(update.effective_user),
            )
            return
        if selected_date == today and _parse_slot(time_str) < datetime.now().time():
            await query.message.reply_text(
                "⏰ Это время уже прошло. Выберите другое время или другой день.",
                reply_markup=get_time_keyboard(selected_date),
            )
            return
        if not booking_store.has_free_master(selected_date, time_str):
            await query.message.reply_text(
                "😔 К сожалению, на это время уже нет свободных мастеров. Выберите другое время."
            )
            return

        current = context.user_data.get("current_booking", {})
        current["date"] = selected_date.isoformat()
        current["time"] = time_str
        context.user_data["current_booking"] = current
        context.user_data["state"] = STATE_SELECTING_MASTER

        await query.message.reply_text(
            f"📅 Дата: {selected_date.strftime('%d.%m.%Y')}\n🕐 Время: {_format_time_display(time_str)}\n\n👩 Выберите мастера:",
            reply_markup=get_masters_keyboard(selected_date, time_str),
        )
        return

    if data.startswith("master:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        _, day_str, rest = parts
        time_str, master_name = rest.rsplit(":", 1)
        selected_date = date.fromisoformat(day_str)
        if booking_store.is_slot_taken(selected_date, time_str, master_name):
            await query.message.reply_text(
                "😔 К сожалению, этот мастер уже занят на это время. Выберите другого мастера."
            )
            return

        current = context.user_data.get("current_booking", {})
        service_ids = current.get("services", [])
        if not service_ids:
            await query.message.reply_text(
                "💅 Сначала выберите услуги перед выбором мастера."
            )
            return

        current["master"] = master_name
        context.user_data["current_booking"] = current

        services_text, total_price, total_duration = summarize_services(service_ids)
        booking_id = f"{int(datetime.now().timestamp())}_{update.effective_user.id}"
        profile = context.user_data.get("profile", {})
        client_name = profile.get("full_name") or update.effective_user.full_name or "—"
        client_phone = profile.get("phone") or "—"

        booking = {
            "id": booking_id,
            "user_id": update.effective_user.id,
            "user_name": update.effective_user.full_name,
            "client_name": client_name,
            "phone": client_phone,
            "services": service_ids,
            "date": selected_date.isoformat(),
            "time": _format_time_display(time_str),
            "master": master_name,
        }
        await asyncio.to_thread(booking_store.add_booking, booking)

        await _notify_manager_new_booking(context, booking, services_text, total_price)

        context.user_data["state"] = STATE_MAIN_MENU

        date_time_str = f"{selected_date.strftime('%d.%m.%Y')}, {_format_time_display(time_str)}"
        text = (
            f"✅ Вы выбрали услуги:\n{services_text}\n\n"
            f"📅 Дата и время: {date_time_str}\n\n"
            f"💰 Общая цена: {total_price} ₽\n\n"
            "Мы записали вас на услугу. Пожалуйста, приходите за 20 минут до сеанса. 🙏\n"
            "Спасибо, что доверяете нам! 💕"
        )

        await query.message.reply_text(
            text,
            reply_markup=get_main_menu_keyboard(update.effective_user),
        )
        return


async def handle_cancel_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("cancel_mode"):
        return

    text = (update.message.text or "").strip()
    context.user_data["cancel_mode"] = False

    user = update.effective_user
    success = await asyncio.to_thread(booking_store.remove_booking, text, user.id)
    if success:
        await update.message.reply_text(
            "✅ Запись успешно отменена.", reply_markup=get_main_menu_keyboard(update.effective_user)
        )
    else:
        await update.message.reply_text(
            "❌ Не удалось найти запись с таким ID. Проверьте правильность и попробуйте ещё раз.",
            reply_markup=get_main_menu_keyboard(update.effective_user),
        )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Токен бота не задан. Установите переменную окружения BOT_TOKEN перед запуском."
        )

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"^\d+_\d+$"),
            handle_cancel_id,
        )
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_handler(CallbackQueryHandler(handle_callback))

    # run_polling() сам управляет циклом событий — не оборачивать в asyncio.run()
    application.run_polling()


if __name__ == "__main__":
    main()

