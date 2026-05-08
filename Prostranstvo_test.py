import asyncio
import json
import os
import re
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, time as dtime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "7740055931"))

ADMINS_FILE = "admins.json"
SETTINGS_FILE = "settings.json"
LOG_FILE = "bot.log"

# =========================
# LOGGING
# =========================
logger = logging.getLogger("botlog")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

async def botlog(text: str):
    logger.info(text)

# =========================
# DEFAULTS
# =========================
_DEFAULT_TEXTS = {
    "welcome_text": "Привет, {name}! Добро пожаловать к нам. 😊\nПодскажи, сколько тебе лет?",
    "consent_text": "Отлично! {age} — прекрасный возраст.\n\nЧтобы мы могли добавить тебя в списки и дать доступ, готов(а) заполнить небольшую анкету?",
    "questionnaire_text": "📝 <b>Шаблон анкеты участника:</b>\n\n1. Как тебя зовут?\n2. Из какого ты города?\n3. Чем увлекаешься?\n\n<i>Скопируй этот текст, заполни свои данные и отправь прямо сюда в чат!</i>",
    "decline_text": "Без проблем! Если позже передумаешь, просто напиши команду /bv в этот чат.",
}

_DEFAULT_SETTINGS = {
    "level": 1,
    "reply_delay": 1,
    "work_start": "07:00",
    "work_end": "19:00",
    "is_active": True,
    "notify_admins": [ADMIN_CHAT_ID],
    "texts": dict(_DEFAULT_TEXTS),
}

# =========================
# FILES
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
        return [int(x) for x in json.load(f)]

def save_admins(admins: list[int]):
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(set(admins)), f, ensure_ascii=False, indent=2)

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
    try:
        lvl = int(s.get("level", 1))
    except:
        lvl = 1
    s["level"] = lvl if lvl in (1, 2, 3) else 1

    try:
        d = int(s.get("reply_delay", 0))
    except:
        d = 0
    s["reply_delay"] = max(0, min(360, d))

    s["is_active"] = bool(s.get("is_active", True))

    if "notify_admins" not in s or not isinstance(s["notify_admins"], list):
        s["notify_admins"] = [ADMIN_CHAT_ID]
    s["notify_admins"] = [int(x) for x in s["notify_admins"]]

    s["work_start"] = str(s.get("work_start", "07:00"))
    s["work_end"] = str(s.get("work_end", "19:00"))

    if "texts" not in s or not isinstance(s["texts"], dict):
        s["texts"] = dict(_DEFAULT_TEXTS)
    for k, v in _DEFAULT_TEXTS.items():
        if k not in s["texts"] or not isinstance(s["texts"][k], str):
            s["texts"][k] = v
    return s

# =========================
# INIT
# =========================
_ensure_files()
ADMIN_USER_IDS = load_admins()
settings_data = normalize_settings(load_settings())

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

class AdminStates(StatesGroup):
    menu = State()
    edit_active = State()
    edit_level = State()
    edit_work = State()
    edit_delay = State()
    view_admins = State()
    add_admin = State()
    remove_admin = State()
    admin_notify_toggle = State()
    texts_menu = State()
    edit_text_value = State()
    logs_n = State()

# =========================
# HELPERS
# =========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

def get_notify_admins() -> list[int]:
    return settings_data.get("notify_admins", []) or []

def get_texts() -> dict:
    return settings_data.get("texts", {})

def parse_hhmm(s: str) -> dtime:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{2}):(\d{2})", s)
    if not m:
        raise ValueError("Неверный формат HH:MM")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Время вне диапазона")
    return dtime(hh, mm)

def is_time_active_now() -> bool:
    if not settings_data.get("is_active", True):
        return False
    try:
        start = parse_hhmm(settings_data["work_start"])
        end = parse_hhmm(settings_data["work_end"])
    except:
        return False
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end

def level_value() -> int:
    try:
        return int(settings_data.get("level", 1))
    except:
        return 1

def bot_is_off_message() -> str:
    return (
        "Бот сейчас не работает.\n"
        f"Время: {settings_data['work_start']} - {settings_data['work_end']}\n"
        "Попробуй позже."
    )

async def typed_delay():
    d = int(settings_data.get("reply_delay", 0))
    if d > 0:
        await asyncio.sleep(d)

def user_profile_link(user_id: int, full_name: str | None) -> str:
    label = full_name or str(user_id)
    return f'<a href="tg://user?id={user_id}">{label}</a>'

async def build_message_link_safe(message: Message) -> str:
    """
    Корректная ссылка на сообщение даже для супергрупп без @username:
    https://t.me/c/<internal_id>/<message_id>
    """
    chat = message.chat
    chat_id = message.chat.id

    # 1) Если у чата есть username
    try:
        if getattr(chat, "username", None):
            return f"https://t.me/{chat.username}/{message.message_id}"
    except:
        pass

    # 2) Иначе — /c/ вариант для chat_id вида -100XXXXXXXXXX
    try:
        if isinstance(chat_id, int) and str(chat_id).startswith("-100"):
            internal_id = abs(chat_id) - 1000000000000
            return f"https://t.me/c/{internal_id}/{message.message_id}"
    except:
        pass

    # 3) fallback
    return f"chat_id={message.chat.id}, message_id={message.message_id}"

def is_suspicious_text(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return (
        "http://" in t
        or "https://" in t
        or "t.me/" in t
        or bool(re.search(r"(^|\s)@[\w_]{5,32}($|\s)", t))
    )

def is_refusal(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    refusals = ["не хочу", "не буду", "отказываюсь", "нет", "не согласен", "против", "отказ"]
    return any(p in t for p in refusals)

# =========================
# BAN & CLEANUP
# =========================
async def do_ban(message: Message, reason: str):
    chat_id, user_id = message.chat.id, message.from_user.id
    await botlog(f"BAN start chat_id={chat_id} user_id={user_id} reason={reason}")
    try:
        await message.delete()
    except:
        pass
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        await botlog(f"BAN failed err={repr(e)}")
        return

    profile_link = user_profile_link(user_id, message.from_user.full_name)
    msg_link = await build_message_link_safe(message)

    for admin_id in get_notify_admins():
        try:
            await bot.send_message(
                admin_id,
                f"🚫 <b>Бан пользователя</b>\n\n"
                f"Пользователь: {profile_link}\n"
                f"Причина: <code>{reason}</code>\n"
                f"Сообщение: {msg_link}"
            )
        except:
            continue

async def maybe_ban_on_suspicious_links(message: Message) -> bool:
    if is_suspicious_text((message.text or "") or (message.caption or "")):
        await do_ban(message, "Подозрительные ссылки/@")
        return True
    return False

async def safe_delete(chat_id: int, message_id: int):
    if message_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except:
            pass

async def ban_with_cleanup(message: Message, reason: str, state: FSMContext):
    chat_id = message.chat.id
    data = await state.get_data()
    to_del = set((data.get("user_message_ids", []) or []) + (data.get("bot_message_ids", []) or []))
    to_del.update([data.get("joined_message_id"), data.get("welcome_message_id")])
    for mid in filter(None, to_del):
        await safe_delete(chat_id, mid)

    await safe_delete(chat_id, message.message_id)
    await do_ban(message, reason)
    await state.clear()

# =========================
# USER FLOW
# =========================
@router.message(F.new_chat_members)
async def welcome_new_member(message: Message):
    if not is_time_active_now() or level_value() not in (1, 2, 3):
        return

    for new_member in message.new_chat_members:
        if new_member.id == bot.id:
            continue

        await typed_delay()

        welcome_text = get_texts()["welcome_text"].format(
            name=f'<a href="tg://user?id={new_member.id}">{new_member.first_name}</a>'
        )

        user_state = FSMContext(
            storage=dp.storage,
            key=StorageKey(bot_id=bot.id, chat_id=message.chat.id, user_id=new_member.id)
        )
        await user_state.set_state(Onboarding.waiting_for_age)

        welcome_msg = await message.reply(welcome_text)
        await user_state.update_data(
            joined_message_id=message.message_id,
            welcome_message_id=welcome_msg.message_id
        )

@router.message(F.left_chat_member)
async def left_chat_member_handler(message: Message):
    await safe_delete(message.chat.id, message.message_id)

@router.message(Onboarding.waiting_for_age, F.text)
async def process_age(message: Message, state: FSMContext):
    if not is_time_active_now():
        return await state.clear()

    if await maybe_ban_on_suspicious_links(message):
        return await state.clear()

    if is_refusal(message.text or ""):
        return await ban_with_cleanup(message, "Отказ назвать возраст", state)

    m = re.search(r"\d{1,3}", message.text or "")
    if not m:
        return await state.clear()

    age = int(m.group(0))

    if age < 18 or age >= 70:
        return await ban_with_cleanup(message, f"Возраст вне диапазона: {age}", state)

    if level_value() == 1:
        return await state.clear()

    await typed_delay()
    await message.reply(get_texts()["consent_text"].format(age=age))
    await state.update_data(age=age)
    await state.set_state(Onboarding.waiting_for_consent)

@router.message(Onboarding.waiting_for_consent, F.text)
async def process_consent(message: Message, state: FSMContext):
    if not is_time_active_now() or await maybe_ban_on_suspicious_links(message):
        return await state.clear()

    text = (message.text or "").lower().strip()
    positive_words = {"да", "давай", "ок", "окей", "хочу", "конечно", "готов", "+"}
    is_agreed = any(w in text for w in positive_words)

    await typed_delay()
    if not is_agreed:
        await message.reply(get_texts()["decline_text"])
        return await state.clear()

    if level_value() == 2:
        return await state.clear()

    await message.reply(get_texts()["questionnaire_text"])
    await state.set_state(Onboarding.waiting_for_questionnaire)

@router.message(Onboarding.waiting_for_questionnaire, F.text)
async def process_questionnaire_done(message: Message, state: FSMContext):
    if not is_time_active_now() or await maybe_ban_on_suspicious_links(message):
        return await state.clear()

    if is_refusal(message.text or ""):
        return await ban_with_cleanup(message, "Отказ заполнять анкету", state)

    for admin_id in get_notify_admins():
        try:
            await bot.send_message(
                admin_id,
                f"✅ Анкета заполнена\n\n"
                f"Пользователь: @{message.from_user.username or message.from_user.id}\n"
                f"Чат: {message.chat.title}"
            )
        except:
            continue

    await message.reply("Отлично! Анкета принята.")
    await state.clear()

@router.message(Command("bv"))
async def bv_cmd(message: Message):
    if not is_time_active_now():
        return await message.reply(bot_is_off_message())
    if level_value() != 3:
        return await message.reply("Команда /bv доступна только при уровне 3.")
    await typed_delay()
    await message.reply(get_texts()["questionnaire_text"])

# =========================
# ADMIN PANEL UI
# =========================
BTN_ADMIN_PANEL = "🛠️ Админ-панель"
BTN_BACK = "◀️ Назад"

ADMIN_OPEN_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_ADMIN_PANEL)]], resize_keyboard=True)
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_BACK)]], resize_keyboard=True)

def admin_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Админы"), KeyboardButton(text="🔔 Оповещения")],
            [KeyboardButton(text="🟢 Бот ON/OFF"), KeyboardButton(text="🏷️ Уровни 1/2/3")],
            [KeyboardButton(text="🕒 Время работы"), KeyboardButton(text="⏱️ Задержка")],
            [KeyboardButton(text="📝 Тексты"), KeyboardButton(text="🧾 Логи")],
            [KeyboardButton(text="❌ Закрыть панель")]
        ],
        resize_keyboard=True
    )

async def admin_display(aid: int) -> str:
    """
    Показываем: @username если есть, иначе имя/title, иначе только id.
    """
    try:
        chat = await bot.get_chat(aid)
        uname = getattr(chat, "username", None)
        first_name = getattr(chat, "first_name", None) or getattr(chat, "title", None)
        if uname:
            return f"@{uname} (<code>{aid}</code>)"
        if first_name:
            return f"{first_name} (<code>{aid}</code>)"
    except:
        pass
    return f"<code>{aid}</code>"

async def admins_lines_with_names() -> str:
    lines = []
    for aid in ADMIN_USER_IDS:
        lines.append(f"• {await admin_display(aid)}")
    return "\n".join(lines) or "Список пуст."

async def show_admin_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.menu)
    await message.reply(
        "🛠️ <b>Админ-панель</b>\n\n"
        f"Бот: <code>{'ON 🟢' if settings_data.get('is_active', True) else 'OFF 🔴'}</code>\n"
        f"Уровень: <code>{settings_data.get('level', 1)}</code>\n"
        f"Время: <code>{settings_data.get('work_start')} - {settings_data.get('work_end')}</code>\n"
        f"Задержка: <code>{settings_data.get('reply_delay')} сек.</code>\n",
        reply_markup=admin_main_kb()
    )

@router.message(F.text.in_([BTN_BACK, "❌ Закрыть панель"]))
async def global_admin_back(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "❌ Закрыть панель":
        await state.clear()
        await message.reply("Панель закрыта.", reply_markup=ReplyKeyboardRemove())
    else:
        await show_admin_menu(message, state)

@router.message(Command("admin"))
@router.message(F.text == BTN_ADMIN_PANEL)
async def admin_open_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await message.reply("⛔ Нет доступа.")
    await show_admin_menu(message, state)

# 1) Админы
@router.message(AdminStates.menu, F.text == "👥 Админы")
async def admin_admins_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.view_admins)
    admins_lines = await admins_lines_with_names()

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="➖ Удалить")],
            [KeyboardButton(text=BTN_BACK)]
        ],
        resize_keyboard=True
    )

    await message.reply(f"<b>👥 Админы</b>\n\n{admins_lines}", reply_markup=kb)

@router.message(AdminStates.view_admins, F.text == "➕ Добавить")
async def admin_add_prepare(message: Message, state: FSMContext):
    await state.set_state(AdminStates.add_admin)
    await message.reply("Введи ID нового админа:", reply_markup=CANCEL_KB)

@router.message(AdminStates.add_admin, F.text)
async def admin_add_finish(message: Message, state: FSMContext):
    try:
        aid = int(message.text.strip())
    except:
        return await message.reply("Неверный ID. Введите число.")

    if aid not in ADMIN_USER_IDS:
        ADMIN_USER_IDS.append(aid)
        save_admins(ADMIN_USER_IDS)
        await message.reply("✅ Админ добавлен.")

    await show_admin_menu(message, state)

@router.message(AdminStates.view_admins, F.text == "➖ Удалить")
async def admin_del_prepare(message: Message, state: FSMContext):
    await state.set_state(AdminStates.remove_admin)
    await message.reply("Введи ID админа для удаления:", reply_markup=CANCEL_KB)

@router.message(AdminStates.remove_admin, F.text)
async def admin_remove_finish(message: Message, state: FSMContext):
    try:
        rid = int(message.text.strip())
    except:
        return await message.reply("Неверный ID. Введите число.")

    if rid in ADMIN_USER_IDS and len(ADMIN_USER_IDS) > 1:
        ADMIN_USER_IDS.remove(rid)
        save_admins(ADMIN_USER_IDS)
        await message.reply("✅ Админ удалён.")
    elif len(ADMIN_USER_IDS) <= 1:
        await message.reply("❌ Нельзя удалить последнего админа.")

    await show_admin_menu(message, state)

# Оповещения
@router.message(AdminStates.menu, F.text == "🔔 Оповещения")
async def admin_notify_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.admin_notify_toggle)
    notify_set = set(get_notify_admins())
    lines = "\n".join(
        [f"• <code>{aid}</code> — {'ON 🔔' if aid in notify_set else 'OFF 🔕'}" for aid in ADMIN_USER_IDS]
    )
    await message.reply(
        f"<b>🔔 Оповещения</b>\n\n{lines}\n\nВведи ID админа для переключения (Вкл/Выкл).",
        reply_markup=CANCEL_KB
    )

@router.message(AdminStates.admin_notify_toggle, F.text)
async def admin_notify_toggle_finish(message: Message, state: FSMContext):
    try:
        aid = int(message.text.strip())
    except:
        return await message.reply("Неверный ID.")

    notify_set = set(get_notify_admins())
    if aid in notify_set:
        notify_set.remove(aid)
    else:
        notify_set.add(aid)

    settings_data["notify_admins"] = sorted(notify_set)
    save_settings(settings_data)

    await message.reply("✅ Оповещения обновлены.")
    await show_admin_menu(message, state)

# 2) Бот ON/OFF
@router.message(AdminStates.menu, F.text == "🟢 Бот ON/OFF")
async def admin_active_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.edit_active)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Включить"), KeyboardButton(text="Выключить")],
            [KeyboardButton(text=BTN_BACK)]
        ],
        resize_keyboard=True
    )
    await message.reply("Управление работой бота:", reply_markup=kb)

@router.message(AdminStates.edit_active, F.text.in_(["Включить", "Выключить"]))
async def admin_active_finish(message: Message, state: FSMContext):
    settings_data["is_active"] = (message.text == "Включить")
    save_settings(settings_data)
    await message.reply(f"✅ Бот {'включен' if settings_data['is_active'] else 'выключен'}.")
    await show_admin_menu(message, state)

# 3) Уровни
@router.message(AdminStates.menu, F.text == "🏷️ Уровни 1/2/3")
async def admin_level_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.edit_level)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1"), KeyboardButton(text="2"), KeyboardButton(text="3")],
            [KeyboardButton(text=BTN_BACK)]
        ],
        resize_keyboard=True
    )
    await message.reply("Выбери уровень работы бота:", reply_markup=kb)

@router.message(AdminStates.edit_level, F.text.in_(["1", "2", "3"]))
async def admin_level_finish(message: Message, state: FSMContext):
    settings_data["level"] = int(message.text)
    save_settings(settings_data)
    await message.reply("✅ Уровень обновлён.")
    await show_admin_menu(message, state)

# 4) Время работы
@router.message(AdminStates.menu, F.text == "🕒 Время работы")
async def admin_work_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.edit_work)
    await message.reply(
        "Отправь время работы в формате <b>ЧЧ:ММ-ЧЧ:ММ</b> (например: <code>07:00-19:00</code>)",
        reply_markup=CANCEL_KB
    )

@router.message(AdminStates.edit_work, F.text)
async def admin_work_finish(message: Message, state: FSMContext):
    try:
        start_str, end_str = message.text.split("-")
        parse_hhmm(start_str)
        parse_hhmm(end_str)
        settings_data["work_start"], settings_data["work_end"] = start_str.strip(), end_str.strip()
        save_settings(settings_data)
        await message.reply("✅ Время обновлено.")
        await show_admin_menu(message, state)
    except:
        await message.reply("❌ Неверный формат. Нужно ЧЧ:ММ-ЧЧ:ММ (например: 07:00-19:00)")

# 5) Задержка
@router.message(AdminStates.menu, F.text == "⏱️ Задержка")
async def admin_delay_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.edit_delay)
    await message.reply("Введи задержку перед ответом бота (от 0 до 360 секунд):", reply_markup=CANCEL_KB)

@router.message(AdminStates.edit_delay, F.text)
async def admin_delay_finish(message: Message, state: FSMContext):
    try:
        v = int(message.text.strip())
        if not (0 <= v <= 360):
            raise ValueError
        settings_data["reply_delay"] = v
        save_settings(settings_data)
        await message.reply("✅ Задержка обновлена.")
        await show_admin_menu(message, state)
    except:
        await message.reply("❌ Введи число от 0 до 360.")

# 6) Тексты
@router.message(AdminStates.menu, F.text == "📝 Тексты")
async def admin_texts_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.texts_menu)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1"), KeyboardButton(text="2")],
            [KeyboardButton(text="3"), KeyboardButton(text="4")],
            [KeyboardButton(text=BTN_BACK)]
        ],
        resize_keyboard=True
    )
    await message.reply(
        "Выбери текст для изменения:\n1) Приветствие (welcome_text)\n2) Согласие (consent_text)\n3) Анкета (questionnaire_text)\n4) Отказ (decline_text)",
        reply_markup=kb
    )

@router.message(AdminStates.texts_menu, F.text.in_(["1", "2", "3", "4"]))
async def admin_texts_pick(message: Message, state: FSMContext):
    mapping = {"1": "welcome_text", "2": "consent_text", "3": "questionnaire_text", "4": "decline_text"}
    key = mapping[message.text]

    await state.update_data(_text_key=key)
    await state.set_state(AdminStates.edit_text_value)

    await message.reply(
        f"Отправь новый текст для <b>{key}</b>.\nТекущий текст:\n<code>{settings_data['texts'][key]}</code>",
        reply_markup=CANCEL_KB
    )

@router.message(AdminStates.edit_text_value, F.text)
async def admin_texts_update(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("_text_key")
    settings_data["texts"][key] = message.text
    save_settings(settings_data)

    await message.reply("✅ Текст обновлён.")
    await show_admin_menu(message, state)

# 7) Логи
@router.message(AdminStates.menu, F.text == "🧾 Логи")
async def admin_logs_menu(message: Message, state: FSMContext):
    await state.set_state(AdminStates.logs_n)
    await message.reply("Сколько последних строк показать? (например: 50)", reply_markup=CANCEL_KB)

@router.message(AdminStates.logs_n, F.text)
async def admin_logs_show(message: Message, state: FSMContext):
    try:
        n = max(1, min(300, int(message.text.strip())))
    except:
        return await message.reply("Введите число от 1 до 300.")

    if not os.path.exists(LOG_FILE):
        lines = ["Логов пока нет."]
    else:
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.rstrip("\n") for ln in f.readlines()[-n:]]

    text = "🧾 <b>Последние логи</b>\n\n" + "\n".join(lines)
    if len(text) > 3900:
        text = "🧾 Логи (укорочено)\n\n" + "\n".join(lines[-100:])

    await message.reply(text)
    await show_admin_menu(message, state)

# =========================
# RUN
# =========================
async def main():
    await botlog("BOT START")
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
