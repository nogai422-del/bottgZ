import asyncio
import json
import os
import re
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, time as dtime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from aiogram.filters import Command

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "7740055931"))

ADMINS_FILE = "admins.json"
SETTINGS_FILE = "settings.json"
LOG_FILE = "bot.log"

# =========================
# LOGGING (для админов)
# =========================
logger = logging.getLogger("botlog")
logger.setLevel(logging.INFO)

if not logger.handlers:
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)


async def botlog(text: str):
    logger.info(text)


def read_last_lines(path: str, n: int = 120) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except FileNotFoundError:
        return "log file not found"


def safe_truncate(s: str, max_len: int = 3900) -> str:
    return s if len(s) <= max_len else s[-max_len:]


# =========================
# DEFAULTS
# =========================
_DEFAULT_TEXTS = {
    "welcome_text": "Привет, {name}! Добро пожаловать к нам. 😊\nПодскажи, сколько тебе лет?",
    "consent_text": "Отлично! {age} — прекрасный возраст.\n\nЧтобы мы могли добавить тебя в списки и дать доступ, готов(а) заполнить небольшую анкету?",
    "questionnaire_text": "📝 <b>Шаблон анкеты участника:</b>\n\n1. Как тебя зовут?\n2. Из какого ты города?\n3. Чем увлекаешься?\n\n<i>Скопируй этот текст, заполни свои данные и отправь прямо сюда в чат!</i>",
    "decline_text": "Без проблем! Если позже передумаешь, просто напиши команду /sv в этот чат.",
}

_DEFAULT_SETTINGS = {
    "mode": 2,  # 0/1/2
    "reply_delay": 1,
    "work_start": "22:00",
    "work_end": "06:00",
    "is_active": True,
    "notify_admins": [ADMIN_CHAT_ID],
    "texts": _DEFAULT_TEXTS,
}

# =========================
# FILE HELPERS
# =========================
def _ensure_files():
    if not os.path.exists(ADMINS_FILE):
        with open(ADMINS_FILE, "w", encoding="utf-8") as f:
            json.dump([ADMIN_CHAT_ID], f, ensure_ascii=False, indent=2)

    if not os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_DEFAULT_SETTINGS, f, ensure_ascii=False, indent=2)


def load_admins() -> list[int]:
    _ensure_files()
    with open(ADMINS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [int(x) for x in data]


def save_admins(admin_ids: list[int]):
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(set(admin_ids)), f, ensure_ascii=False, indent=2)


def load_settings() -> dict:
    _ensure_files()
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_settings(settings: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def normalize_settings(data: dict) -> dict:
    s = dict(_DEFAULT_SETTINGS)
    s.update(data or {})

    if "texts" not in s or not isinstance(s["texts"], dict):
        s["texts"] = dict(_DEFAULT_TEXTS)

    for k, v in _DEFAULT_TEXTS.items():
        if k not in s["texts"] or not isinstance(s["texts"][k], str):
            s["texts"][k] = v

    if "notify_admins" not in s or not isinstance(s["notify_admins"], list):
        s["notify_admins"] = [ADMIN_CHAT_ID]

    s["notify_admins"] = [int(x) for x in s["notify_admins"]]
    s["mode"] = int(s.get("mode", 2))
    s["reply_delay"] = int(s.get("reply_delay", 1))
    s["is_active"] = bool(s.get("is_active", True))
    return s


# =========================
# INIT
# =========================
ADMIN_USER_IDS = load_admins()
settings_data = normalize_settings(load_settings())

# =========================
# ADMINS name cache (anti-freeze)
# =========================
_admins_cache: dict[int, str] = {}
_admins_cache_ts: float = 0.0
ADMINS_CACHE_TTL_SECONDS = 3600  # 1 час

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()

# =========================
# STATES
# =========================
class Onboarding(StatesGroup):
    waiting_for_age = State()
    waiting_for_consent = State()
    waiting_for_questionnaire = State()


class AdminEdit(StatesGroup):
    editing_welcome = State()
    editing_consent = State()
    editing_questionnaire = State()
    editing_decline = State()
    adding_admin = State()
    removing_admin = State()
    setting_notify_admins = State()
    setting_mode = State()
    editing_work_start = State()
    editing_work_end = State()


# =========================
# HELPERS
# =========================
async def typed_delay(delay: float):
    if delay and delay > 0:
        await asyncio.sleep(delay)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def parse_hhmm(s: str) -> dtime:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{2}):(\d{2})", s)
    if not m:
        raise ValueError("Неверный формат времени. Используй HH:MM (например 22:00)")
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError("Время вне диапазона")
    return dtime(hh, mm)


def bot_is_off_by_time() -> bool:
    if not settings_data.get("is_active", True):
        return True
    try:
        start = parse_hhmm(settings_data["work_start"])
        end = parse_hhmm(settings_data["work_end"])
    except Exception:
        return False

    now = datetime.now().time()
    if start <= end:
        return not (start <= now <= end)
    return not (now >= start or now <= end)


def bot_is_running_for_welcome():
    if bot_is_off_by_time():
        return False
    return int(settings_data.get("mode", 2)) != 0


def bot_is_running_for_questionnaire():
    if bot_is_off_by_time():
        return False
    return int(settings_data.get("mode", 2)) == 2


def get_texts():
    return settings_data.get("texts", {})


def get_notify_admins():
    return settings_data.get("notify_admins", []) or []


def bot_is_off_message() -> str:
    return (
        "Бот сейчас не работает.\n"
        f"Интервал: {settings_data['work_start']} - {settings_data['work_end']}\n"
        f"Уровень: {settings_data.get('mode', 2)}\n\n"
        "Попробуй позже."
    )


def persist_all():
    save_settings(settings_data)


# =========================
# Suspicious detection + BAN
# =========================
def is_suspicious_text(text: str) -> bool:
    if not text:
        return False
    t = text.lower()

    if "http://" in t or "https://" in t:
        return True
    if "t.me/" in t:
        return True

    return bool(re.search(r"(^|\s)@[\w_]{5,32}($|\s)", t))


async def do_ban(message: Message, reason: str):
    chat_id = message.chat.id
    user_id = message.from_user.id

    await botlog(f"BAN start chat_id={chat_id} user_id={user_id} reason={reason}")

    # delete best-effort
    try:
        await message.delete()
        await botlog(
            f"DELETE ok message_id={message.message_id} chat_id={chat_id} user_id={user_id}"
        )
    except Exception as e:
        await botlog(
            f"DELETE failed message_id={message.message_id} chat_id={chat_id} user_id={user_id} err={repr(e)}"
        )

    # ban
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await botlog(f"BAN ok chat_id={chat_id} user_id={user_id}")
    except Exception as e:
        await botlog(f"BAN failed chat_id={chat_id} user_id={user_id} err={repr(e)}")
        return

    # delete again
    try:
        await message.delete()
        await botlog(
            f"DELETE(2) ok message_id={message.message_id} chat_id={chat_id} user_id={user_id}"
        )
    except Exception as e:
        await botlog(
            f"DELETE(2) failed message_id={message.message_id} chat_id={chat_id} user_id={user_id} err={repr(e)}"
        )

    # notify admins list
    profile_link = f'<a href="tg://user?id={user_id}">профиль</a>'
    for admin_id in get_notify_admins():
        try:
            await bot.send_message(
                admin_id,
                "🚫 Бан пользователя\n\n"
                f"Пользователь: {profile_link}\n"
                f"ID: <code>{user_id}</code>\n"
                f"Причина: {reason}\n"
                f"Чат: <code>{chat_id}</code>",
            )
        except TelegramBadRequest:
            continue
        except Exception:
            continue


async def maybe_ban_on_suspicious_links(message: Message) -> bool:
    raw_text = (message.text or "") or (message.caption or "")
    if not raw_text:
        return False
    if not is_suspicious_text(raw_text):
        return False

    await botlog(
        f"DETECT suspicious user_id={message.from_user.id} chat_id={message.chat.id} text={raw_text[:220]!r}"
    )
    await do_ban(message, "Подозрительные ссылки/@")
    return True


# =========================
# ADMIN PANEL (красиво + список админов + тексты)
# =========================
def back_to_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:panel")]]
    )


def _mode_label() -> str:
    m = int(settings_data.get("mode", 2))
    return "Уровень 0" if m == 0 else "Уровень 1" if m == 1 else "Уровень 2"


def _status_label() -> str:
    active = settings_data.get("is_active", True)
    off_by_time = bot_is_off_by_time()
    if active and not off_by_time:
        return "🟢 Включен"
    return "🔴 Выключен/время off"


async def format_admins_list(limit: int = 50) -> str:
    """
    Anti-freeze:
    - кэш имён
    - максимум 8 get_chat на один callback
    - wait_for таймаут
    """
    global _admins_cache, _admins_cache_ts

    now_ts = datetime.now().timestamp()
    if not _admins_cache or (now_ts - _admins_cache_ts) > ADMINS_CACHE_TTL_SECONDS:
        _admins_cache = {}
        _admins_cache_ts = now_ts

    if not ADMIN_USER_IDS:
        return "Список админов пуст."

    hard_limit = min(limit, 8)
    lines: list[str] = []

    for i, uid in enumerate(ADMIN_USER_IDS[:hard_limit], start=1):
        name = _admins_cache.get(uid)
        if not name:
            try:
                chat = await asyncio.wait_for(bot.get_chat(uid), timeout=4.0)
                title = getattr(chat, "full_name", None) or getattr(chat, "first_name", None)
                if title:
                    name = title
                else:
                    uname = getattr(chat, "username", None)
                    name = f"@{uname}" if uname else "—"
            except Exception:
                name = "—"
            _admins_cache[uid] = name

        lines.append(f"{i}. <code>{uid}</code> — {name}")

    total = len(ADMIN_USER_IDS)
    if total > hard_limit:
        lines.append(f"… и ещё {total - hard_limit} админ(ов) (показываю первые {hard_limit}).")

    return "\n".join(lines)


def build_admin_panel() -> InlineKeyboardMarkup:
    delay = settings_data.get("reply_delay", 1)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Статус: {_status_label()}", callback_data="admin:noop")],
            [
                InlineKeyboardButton(
                    text=f"Интервал: {settings_data['work_start']}–{settings_data['work_end']}",
                    callback_data="admin:work_info",
                )
            ],
            [InlineKeyboardButton(text=f"{_mode_label()}", callback_data="admin:mode_info")],
            [
                InlineKeyboardButton(text="−1c", callback_data="admin:delay_minus"),
                InlineKeyboardButton(text=f"Задержка: {delay}с", callback_data="admin:delay_info"),
                InlineKeyboardButton(text="+1c", callback_data="admin:delay_plus"),
            ],
            [InlineKeyboardButton(text="📝 Тексты", callback_data="admin:view_texts")],
            [InlineKeyboardButton(text="👮 Админы", callback_data="admin:view_admins")],
            [
                InlineKeyboardButton(text="✏️ Приветствие", callback_data="admin:edit_welcome"),
                InlineKeyboardButton(text="✏️ Согласие", callback_data="admin:edit_consent"),
            ],
            [
                InlineKeyboardButton(text="✏️ Анкета", callback_data="admin:edit_questionnaire"),
                InlineKeyboardButton(text="✏️ Отказ", callback_data="admin:edit_decline"),
            ],
            [
                InlineKeyboardButton(text="⏱ Старт", callback_data="admin:edit_work_start"),
                InlineKeyboardButton(text="⏱ Конец", callback_data="admin:edit_work_end"),
            ],
            [
                InlineKeyboardButton(text="➕ Админ", callback_data="admin:add_admin"),
                InlineKeyboardButton(text="➖ Убрать", callback_data="admin:remove_admin"),
            ],
            [
                InlineKeyboardButton(text="🔔 Оповещения", callback_data="admin:edit_notify_admins"),
                InlineKeyboardButton(text="⚙️ Уровень", callback_data="admin:edit_mode"),
            ],
            [InlineKeyboardButton(text="🟣 Toggle is_active", callback_data="admin:toggle_active")],
            [InlineKeyboardButton(text="🧾 Логи (/botlog)", callback_data="admin:noop")],
        ]
    )


@router.message(Command("panel"))
async def admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "⚙️ <b>Панель управления ботом</b>\n\n"
        f"• {_status_label()}\n"
        f"• Уровень: <code>{settings_data.get('mode', 2)}</code>\n"
        f"• Интервал: <code>{settings_data['work_start']}–{settings_data['work_end']}</code>\n"
        f"• Задержка: <code>{settings_data.get('reply_delay', 1)}</code> сек\n\n"
        "Выбирай действие ниже:",
        reply_markup=build_admin_panel(),
    )


@router.callback_query(F.data == "admin:panel")
async def cb_back_to_panel(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    try:
        await call.message.edit_text(
            "⚙️ <b>Панель управления ботом</b>\n\n"
            f"• {_status_label()}\n"
            f"• Уровень: <code>{settings_data.get('mode', 2)}</code>\n"
            f"• Интервал: <code>{settings_data['work_start']}–{settings_data['work_end']}</code>\n"
            f"• Задержка: <code>{settings_data.get('reply_delay', 1)}</code> сек\n\n"
            "Выбирай действие ниже:",
            reply_markup=build_admin_panel(),
        )
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data == "admin:noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data == "admin:view_admins")
async def cb_view_admins(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return

    # ✅ важно: отвечаем сразу, чтобы callback не "завис"
    await call.answer()

    admins_text = await format_admins_list()

    await state.clear()
    try:
        await call.message.edit_text(
            "👮 <b>Админы бота</b>\n\n"
            "Формат: номер. <code>ID</code> — имя/ник\n\n"
            f"<pre>{admins_text}</pre>",
            reply_markup=back_to_panel_kb(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "admin:view_texts")
async def cb_view_texts(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return

    await call.answer()

    texts = get_texts()

    def fmt_block(key: str, value: str) -> str:
        v = (value or "").strip()
        if len(v) > 1200:
            v = v[:1200] + "…"
        return f"• <code>{key}</code>\n{v}\n"

    body = (
        "📝 <b>Тексты и заготовки</b>\n\n"
        + fmt_block("welcome_text", texts.get("welcome_text", ""))
        + fmt_block("consent_text", texts.get("consent_text", ""))
        + fmt_block("questionnaire_text", texts.get("questionnaire_text", ""))
        + fmt_block("decline_text", texts.get("decline_text", ""))
    )

    await state.clear()
    try:
        await call.message.edit_text(body, reply_markup=back_to_panel_kb())
    except Exception:
        pass


@router.callback_query(F.data == "admin:toggle_active")
async def cb_toggle_active(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    settings_data["is_active"] = not settings_data.get("is_active", True)
    persist_all()
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=build_admin_panel())
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "admin:delay_minus")
async def cb_delay_minus(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    settings_data["reply_delay"] = max(0, int(settings_data.get("reply_delay", 1)) - 1)
    persist_all()
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=build_admin_panel())
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "admin:delay_plus")
async def cb_delay_plus(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    if int(settings_data.get("reply_delay", 1)) < 180:
        settings_data["reply_delay"] = int(settings_data.get("reply_delay", 1)) + 1
        persist_all()
    await call.answer()
    try:
        await call.message.edit_reply_markup(reply_markup=build_admin_panel())
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "admin:delay_info")
async def cb_delay_info(call: CallbackQuery):
    await call.answer(f"Текущая задержка: {settings_data.get('reply_delay', 1)} сек", show_alert=True)


@router.callback_query(F.data == "admin:work_info")
async def cb_work_info(call: CallbackQuery):
    await call.answer(
        f"Интервал работы: {settings_data['work_start']} - {settings_data['work_end']}\nПоддерживается через полночь.",
        show_alert=True,
    )


@router.callback_query(F.data == "admin:mode_info")
async def cb_mode_info(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.setting_mode)
    await call.answer()
    await call.message.edit_text(
        "🎚️ <b>Уровень работы бота</b>\n\n"
        "0 — бот выключен\n"
        "1 — спрашивает только возраст\n"
        "2 — выполняет все шаги\n\n"
        f"Текущий: <code>{settings_data.get('mode', 2)}</code>\n\n"
        "Отправь: 0 / 1 / 2",
        reply_markup=back_to_panel_kb(),
    )


@router.message(AdminEdit.setting_mode)
async def set_mode(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    t = (message.text or "").strip()
    if t not in {"0", "1", "2"}:
        await message.answer("Отправь только 0, 1 или 2.")
        return
    settings_data["mode"] = int(t)
    persist_all()
    await state.clear()
    await message.answer("✅ Уровень обновлён.", reply_markup=build_admin_panel())


# ===== Edit texts =====
@router.callback_query(F.data == "admin:edit_welcome")
async def cb_edit_welcome(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_welcome)
    await call.answer()
    await call.message.edit_text(
        "✏️ <b>Редактирование приветствия</b>\n\n"
        "Отправь новый текст.\n"
        "Плейсхолдеры: {name}",
        reply_markup=back_to_panel_kb(),
    )


@router.callback_query(F.data == "admin:edit_consent")
async def cb_edit_consent(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_consent)
    await call.answer()
    await call.message.edit_text(
        "✏️ <b>Редактирование согласия</b>\n\n"
        "Отправь новый текст.\n"
        "Плейсхолдеры: {age}",
        reply_markup=back_to_panel_kb(),
    )


@router.callback_query(F.data == "admin:edit_questionnaire")
async def cb_edit_questionnaire(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_questionnaire)
    await call.answer()
    await call.message.edit_text(
        "✏️ <b>Редактирование текста анкеты</b>\n\n"
        "Отправь новый текст.",
        reply_markup=back_to_panel_kb(),
    )


@router.callback_query(F.data == "admin:edit_decline")
async def cb_edit_decline(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_decline)
    await call.answer()
    await call.message.edit_text(
        "✏️ <b>Редактирование текста отказа</b>\n\n"
        "Отправь новый текст.",
        reply_markup=back_to_panel_kb(),
    )


@router.message(AdminEdit.editing_welcome)
async def save_welcome(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    settings_data["texts"]["welcome_text"] = message.text or ""
    persist_all()
    await state.clear()
    await message.answer("✅ Приветствие обновлено.", reply_markup=build_admin_panel())


@router.message(AdminEdit.editing_consent)
async def save_consent(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    settings_data["texts"]["consent_text"] = message.text or ""
    persist_all()
    await state.clear()
    await message.answer("✅ Согласие обновлено.", reply_markup=build_admin_panel())


@router.message(AdminEdit.editing_questionnaire)
async def save_questionnaire(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    settings_data["texts"]["questionnaire_text"] = message.text or ""
    persist_all()
    await state.clear()
    await message.answer("✅ Анкета обновлена.", reply_markup=build_admin_panel())


@router.message(AdminEdit.editing_decline)
async def save_decline(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    settings_data["texts"]["decline_text"] = message.text or ""
    persist_all()
    await state.clear()
    await message.answer("✅ Отказ обновлён.", reply_markup=build_admin_panel())


# ===== Work times =====
@router.callback_query(F.data == "admin:edit_work_start")
async def cb_edit_work_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_work_start)
    await call.answer()
    await call.message.edit_text(
        "⏱ <b>Редактирование времени СТАРТА</b>\n\n"
        "Отправь HH:MM (например 22:00).",
        reply_markup=back_to_panel_kb(),
    )


@router.callback_query(F.data == "admin:edit_work_end")
async def cb_edit_work_end(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_work_end)
    await call.answer()
    await call.message.edit_text(
        "⏱ <b>Редактирование времени КОНЦА</b>\n\n"
        "Отправь HH:MM (например 06:00).",
        reply_markup=back_to_panel_kb(),
    )


@router.message(AdminEdit.editing_work_start)
async def set_work_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        parse_hhmm(message.text or "")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return
    settings_data["work_start"] = (message.text or "").strip()
    persist_all()
    await state.clear()
    await message.answer("✅ Старт обновлён.", reply_markup=build_admin_panel())


@router.message(AdminEdit.editing_work_end)
async def set_work_end(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        parse_hhmm(message.text or "")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
        return
    settings_data["work_end"] = (message.text or "").strip()
    persist_all()
    await state.clear()
    await message.answer("✅ Конец обновлён.", reply_markup=build_admin_panel())


# ===== Admin management (ID/username) =====
async def resolve_user_id(query: str) -> int:
    q = (query or "").strip()
    if re.fullmatch(r"\d+", q):
        return int(q)
    if q.startswith("@"):
        q = q[1:]
    chat = await bot.get_chat(q)
    return int(chat.id)


@router.callback_query(F.data == "admin:add_admin")
async def cb_add_admin(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.adding_admin)
    await call.answer()
    await call.message.edit_text(
        "➕ <b>Добавить админа</b>\n\n"
        "Отправь ID или username (например: 12345 или @username).",
        reply_markup=back_to_panel_kb(),
    )


@router.callback_query(F.data == "admin:remove_admin")
async def cb_remove_admin(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.removing_admin)
    await call.answer()
    await call.message.edit_text(
        "➖ <b>Убрать админа</b>\n\n"
        "Отправь ID или username (например: 12345 или @username).",
        reply_markup=back_to_panel_kb(),
    )


@router.message(AdminEdit.adding_admin)
async def adding_admin_handler(message: Message, state: FSMContext):
    global _admins_cache_ts, _admins_cache
    if not is_admin(message.from_user.id):
        return
    try:
        uid = await resolve_user_id(message.text or "")
    except Exception as e:
        await message.answer(f"❌ Не могу определить пользователя: {e}")
        return

    if uid in ADMIN_USER_IDS:
        await message.answer("Этот пользователь уже админ.")
        await state.clear()
        return

    ADMIN_USER_IDS.append(uid)
    save_admins(ADMIN_USER_IDS)

    # сброс кэша имён
    _admins_cache = {}
    _admins_cache_ts = 0.0

    await state.clear()
    await message.answer("✅ Админ добавлен.", reply_markup=build_admin_panel())


@router.message(AdminEdit.removing_admin)
async def removing_admin_handler(message: Message, state: FSMContext):
    global _admins_cache_ts, _admins_cache
    if not is_admin(message.from_user.id):
        return
    try:
        uid = await resolve_user_id(message.text or "")
    except Exception as e:
        await message.answer(f"❌ Не могу определить пользователя: {e}")
        return

    if uid not in ADMIN_USER_IDS:
        await message.answer("Этот пользователь не админ.")
        await state.clear()
        return

    ADMIN_USER_IDS[:] = [x for x in ADMIN_USER_IDS if x != uid]
    save_admins(ADMIN_USER_IDS)

    # сброс кэша имён
    _admins_cache = {}
    _admins_cache_ts = 0.0

    await state.clear()
    await message.answer("✅ Админ удалён.", reply_markup=build_admin_panel())


# ===== notify admins list =====
@router.callback_query(F.data == "admin:edit_notify_admins")
async def cb_edit_notify_admins(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.setting_notify_admins)
    await call.answer()
    await call.message.edit_text(
        "🔔 <b>Админы оповещений</b>\n\n"
        "Пришли ID или username.\n"
        "Повтор — уберёт из списка.",
        reply_markup=back_to_panel_kb(),
    )


@router.message(AdminEdit.setting_notify_admins)
async def setting_notify_admins_handler(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        uid = await resolve_user_id(message.text or "")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    cur = set(get_notify_admins())
    if uid in cur:
        cur.remove(uid)
    else:
        cur.add(uid)

    if not cur:
        await message.answer("❌ Список не может быть пустым.")
        return

    settings_data["notify_admins"] = list(cur)
    persist_all()
    await state.clear()
    await message.answer("✅ Список обновлён.", reply_markup=build_admin_panel())


# ===== Botlog commands =====
@router.message(Command("botlog"))
async def botlog_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    log_text = read_last_lines(LOG_FILE, n=160)
    await message.answer(
        "📋 <b>Логи бота</b>\n\n" f"<pre>{safe_truncate(log_text)}</pre>"
    )


@router.message(Command("botlog_clear"))
async def botlog_clear_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")
    await botlog(f"LOG CLEAR by admin_id={message.from_user.id}")
    await message.answer("✅ Логи очищены.")


# =========================
# USER SCENARIO
# =========================
@router.message(F.new_chat_members)
async def welcome_new_member(message: Message):
    if not bot_is_running_for_welcome():
        return

    for new_member in message.new_chat_members:
        if new_member.id == bot.id:
            continue

        await typed_delay(float(settings_data.get("reply_delay", 1)))

        texts = get_texts()
        user_link = f'<a href="tg://user?id={new_member.id}">{new_member.first_name}</a>'
        welcome_text = texts["welcome_text"].format(name=user_link)
        await message.reply(welcome_text)

        user_state = FSMContext(
            storage=dp.storage,
            key=StorageKey(bot_id=bot.id, chat_id=message.chat.id, user_id=new_member.id),
        )
        await user_state.set_state(Onboarding.waiting_for_age)
        await botlog(f"WELCOME sent user_id={new_member.id} chat_id={message.chat.id}")


@router.message(Onboarding.waiting_for_age, F.text)
async def process_age(message: Message, state: FSMContext):
    if not bot_is_running_for_welcome():
        await message.reply(bot_is_off_message())
        await state.clear()
        return

    await botlog(f"AGE step user_id={message.from_user.id} chat_id={message.chat.id}")

    if await maybe_ban_on_suspicious_links(message):
        await state.clear()
        return

    text = message.text or ""
    match = re.search(r"\d+", text.lower())
    if not match:
        await typed_delay(float(settings_data.get("reply_delay", 1)))
        await message.reply("Не совсем понял цифру. Напиши, пожалуйста, возраст числом 😊")
        await botlog(f"AGE parse failed user_id={message.from_user.id}")
        return

    age = int(match.group())

    if age < 18:
        await do_ban(message, f"Возраст меньше 18: {age}")
        await state.clear()
        return

    if age >= 70:
        await do_ban(message, f"Возраст 70+ : {age}")
        await state.clear()
        return

    if int(settings_data.get("mode", 2)) == 1:
        await state.clear()
        return

    await typed_delay(float(settings_data.get("reply_delay", 1)))
    await message.reply(get_texts()["consent_text"].format(age=age))
    await state.set_state(Onboarding.waiting_for_consent)
    await botlog(f"CONSENT sent age={age} user_id={message.from_user.id}")


@router.message(Onboarding.waiting_for_consent, F.text)
async def process_consent(message: Message, state: FSMContext):
    if not bot_is_running_for_welcome():
        await message.reply(bot_is_off_message())
        await state.clear()
        return

    await botlog(f"CONSENT step user_id={message.from_user.id} chat_id={message.chat.id}")

    if await maybe_ban_on_suspicious_links(message):
        await state.clear()
        return

    text = (message.text or "").lower().strip()
    positive_words = [
        "да", "давай", "ок", "окей", "хочу", "+", "конечно", "угу", "ага", "yes", "ладно",
        "готов", "готова", "го", "погнали", "ну давай", "давай попробуем", "ладно давай",
        "попробую", "почему бы и нет, давай", "почему бы и нет, даай", "вай нот", "гоу",
        "летс",
    ]
    is_agreed = any(w in text.split() for w in positive_words) or text in positive_words

    await typed_delay(float(settings_data.get("reply_delay", 1)))

    if is_agreed:
        await message.reply(get_texts()["questionnaire_text"])
        await state.set_state(Onboarding.waiting_for_questionnaire)
        await botlog(f"QUESTIONNAIRE sent user_id={message.from_user.id}")
    else:
        await message.reply(get_texts()["decline_text"])
        await state.clear()
        await botlog(f"DECLINE -> state cleared user_id={message.from_user.id}")


@router.message(Onboarding.waiting_for_questionnaire, F.text)
async def process_questionnaire_done(message: Message, state: FSMContext):
    if await maybe_ban_on_suspicious_links(message):
        await state.clear()
        return

    text = (message.text or "").strip().lower()
    triggers = [
        "заполнил", "заполнила", "заполнено", "готов", "готова", "готово",
        "анкета готова", "анкета заполнена", "анкета готовa", "я заполнил",
        "я заполнила", "отправил", "отправила", "сдал", "сдала", "заполнена анкета",
    ]
    if not any(t in text for t in triggers):
        return

    username_display = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name

    await botlog(f"QUESTIONNAIRE accepted user_id={message.from_user.id}")

    for admin_id in get_notify_admins():
        try:
            await bot.send_message(
                admin_id,
                "Анкета заполнена\n\n"
                f"Кто: {username_display} (ID: <code>{message.from_user.id}</code>)\n"
                f"Чат: {message.chat.title}\n\n"
                "Триггер: анкета/готово",
            )
        except Exception:
            continue

    await state.clear()
    await message.reply("Отлично! Анкета принята.")
    await botlog(f"STATE cleared after questionnaire user_id={message.from_user.id}")


@router.message(Command("sv"))
async def send_questionnaire_cmd(message: Message):
    if not bot_is_running_for_questionnaire():
        await message.reply(bot_is_off_message())
        return
    await typed_delay(float(settings_data.get("reply_delay", 1)))
    await message.reply(get_texts()["questionnaire_text"])
    await botlog(f"/sv sent user_id={message.from_user.id} chat_id={message.chat.id}")


# =========================
# RUN
# =========================
async def main():
    await botlog("BOT START")
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
