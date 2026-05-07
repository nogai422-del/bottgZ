import asyncio
import json
import os
import re
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
ADMIN_CHAT_ID = 7740055931

ADMINS_FILE = "admins.json"
SETTINGS_FILE = "settings.json"

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
    "mode": 2,  # 0/1/2 (как у тебя)
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

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()
router = Router()


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
    # crosses midnight
    return not (now >= start or now <= end)


def is_mode_0():
    return int(settings_data.get("mode", 2)) == 0


def is_mode_1():
    return int(settings_data.get("mode", 2)) == 1


def is_mode_2():
    return int(settings_data.get("mode", 2)) == 2


def bot_is_running_for_welcome():
    if bot_is_off_by_time():
        return False
    if is_mode_0():
        return False
    return True


def bot_is_running_for_questionnaire():
    if bot_is_off_by_time():
        return False
    return is_mode_2()


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


def typed_link_user(user_id: int) -> str:
    return f'<a href="tg://user?id={user_id}">профиль</a>'


# =========================
# Suspicious detection
# =========================
def is_suspicious_text(text: str) -> bool:
    if not text:
        return False
    t = text.lower()

    if "http://" in t or "https://" in t:
        return True
    if "t.me/" in t:
        return True

    # vk / telegram / любые упоминания доменов можно расширить так:
    # if "vk.com" in t or "vk.ru" in t or "vktarget" in t: return True

    # @username (минимум 5 символов)
    return bool(re.search(r"(^|\s)@[\w_]{5,32}($|\s)", t))

async def maybe_ban_on_suspicious_links(message: Message) -> bool:
    # Собираем возможный текст из разных полей
    raw_text = (
        (message.text or "")
        or (message.caption or "")
    )

    if not raw_text:
        return False

    # Лог (в консоль)
    print("DETECT suspicious:", message.from_user.id, "raw_text=", raw_text[:200])

    if not is_suspicious_text(raw_text):
        return False

    await do_ban(message, "Подозрительные ссылки/@")
    return True

# =========================
# BAN + notify
# =========================
async def do_ban(message: Message, reason: str):
    chat_id = message.chat.id
    user_id = message.from_user.id

    # Ссылка на профиль
    profile_link = f'<a href="tg://user?id={user_id}">профиль</a>'

    # 1) БАН и удаление: пробуем удалить текущее сообщение
    try:
        await message.delete()
    except Exception as e:
        # часто бывает "can't delete message" — тогда просто продолжаем
        print("DELETE failed:", e)

    # 2) Бан
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        print("BAN failed:", e)
        return

    # 3) После бана ещё раз попробуем удалить (иногда после бана удаление проходит)
    try:
        await message.delete()
    except Exception:
        pass

    # 4) Оповещаем админов
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
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                continue
            raise
        except Exception:
            continue


# =========================
# ADMIN PANEL (минимально как было)
# =========================
def back_to_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад к панели", callback_data="admin:panel")]]
    )


def build_admin_panel() -> InlineKeyboardMarkup:
    delay = settings_data.get("reply_delay", 1)
    mode = int(settings_data.get("mode", 2))
    mode_label = "Уровень 0" if mode == 0 else "Уровень 1" if mode == 1 else "Уровень 2"

    status = "Включен" if (settings_data.get("is_active", True) and not bot_is_off_by_time()) else "Выключен/время off"
    work = f"{settings_data['work_start']} - {settings_data['work_end']}"

    keyboard = [
        [InlineKeyboardButton(text=f"Статус: {status}", callback_data="admin:noop")],
        [
            InlineKeyboardButton(text="−1с", callback_data="admin:delay_minus"),
            InlineKeyboardButton(text=f"Задержка: {delay} сек", callback_data="admin:delay_info"),
            InlineKeyboardButton(text="+1с", callback_data="admin:delay_plus"),
        ],
        [InlineKeyboardButton(text=work, callback_data="admin:work_info")],
        [InlineKeyboardButton(text=f"{mode_label}", callback_data="admin:mode_info")],
        [
            InlineKeyboardButton(text="Старт", callback_data="admin:edit_work_start"),
            InlineKeyboardButton(text="Конец", callback_data="admin:edit_work_end"),
        ],
        [
            InlineKeyboardButton(text="Назначить админа", callback_data="admin:add_admin"),
            InlineKeyboardButton(text="Убрать админа", callback_data="admin:remove_admin"),
        ],
        [InlineKeyboardButton(text="Админы оповещений", callback_data="admin:edit_notify_admins")],
        [
            InlineKeyboardButton(text="Изменить приветствие", callback_data="admin:edit_welcome"),
            InlineKeyboardButton(text="Изменить согласие", callback_data="admin:edit_consent"),
        ],
        [
            InlineKeyboardButton(text="Изменить текст анкеты", callback_data="admin:edit_questionnaire"),
            InlineKeyboardButton(text="Изменить текст отказа", callback_data="admin:edit_decline"),
        ],
        [
            InlineKeyboardButton(text="Посмотреть все тексты", callback_data="admin:view_texts"),
        ],
        [
            InlineKeyboardButton(text="Toggle is_active", callback_data="admin:toggle"),
            InlineKeyboardButton(text="Рестарт не нужен", callback_data="admin:noop"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.message(Command("panel"))
async def admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "Панель управления ботом\n\n"
        "Тексты и параметры сохраняются.\n"
        f"Интервал: {settings_data['work_start']} - {settings_data['work_end']}",
        reply_markup=build_admin_panel(),
    )


@router.callback_query(F.data == "admin:panel")
async def cb_back_to_panel(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(
        "Панель управления ботом\n\n"
        f"Интервал: {settings_data['work_start']} - {settings_data['work_end']}",
        reply_markup=build_admin_panel(),
    )


@router.callback_query(F.data == "admin:noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data == "admin:toggle")
async def cb_toggle(call: CallbackQuery):
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
    if int(settings_data.get("reply_delay", 1)) < 30:
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
        "Уровень работы бота\n\n"
        "0 — бот выключен\n"
        "1 — спрашивает только возраст\n"
        "2 — выполняет все шаги\n\n"
        f"Текущий: <code>{settings_data.get('mode', 2)}</code>\n\n"
        "Отправь 0/1/2.",
        reply_markup=back_to_panel_kb(),
    )


@router.message(AdminEdit.setting_mode)
async def set_mode(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    t = (message.text or "").strip()
    if t not in {"0", "1", "2"}:
        await message.answer("Отправь 0, 1 или 2.")
        return
    settings_data["mode"] = int(t)
    persist_all()
    await state.clear()
    await message.answer("Уровень обновлён.", reply_markup=build_admin_panel())


# -----------------
# Minimal admin setters for texts (остальное можешь оставить из твоей версии)
# -----------------

@router.callback_query(F.data == "admin:edit_welcome")
async def cb_edit_welcome(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_welcome)
    texts = get_texts()
    await call.message.edit_text(
        "Редактирование приветствия\n\nОтправь новый текст.\nПлейсхолдер: {name}",
        reply_markup=back_to_panel_kb(),
    )
    # (текущий текст не обязателен)


@router.callback_query(F.data == "admin:edit_consent")
async def cb_edit_consent(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_consent)
    await call.message.edit_text(
        "Редактирование согласия\n\nОтправь новый текст.\nПлейсхолдер: {age}",
        reply_markup=back_to_panel_kb(),
    )


@router.callback_query(F.data == "admin:edit_questionnaire")
async def cb_edit_questionnaire(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_questionnaire)
    await call.message.edit_text(
        "Редактирование анкеты\n\nОтправь новый текст.",
        reply_markup=back_to_panel_kb(),
    )


@router.callback_query(F.data == "admin:edit_decline")
async def cb_edit_decline(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminEdit.editing_decline)
    await call.message.edit_text(
        "Редактирование отказа\n\nОтправь новый текст.",
        reply_markup=back_to_panel_kb(),
    )


@router.message(AdminEdit.editing_welcome)
async def save_welcome(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    settings_data["texts"]["welcome_text"] = message.text or ""
    persist_all()
    await state.clear()
    await message.answer("Приветствие обновлено!", reply_markup=build_admin_panel())


@router.message(AdminEdit.editing_consent)
async def save_consent(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    settings_data["texts"]["consent_text"] = message.text or ""
    persist_all()
    await state.clear()
    await message.answer("Согласие обновлено!", reply_markup=build_admin_panel())


@router.message(AdminEdit.editing_questionnaire)
async def save_questionnaire(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    settings_data["texts"]["questionnaire_text"] = message.text or ""
    persist_all()
    await state.clear()
    await message.answer("Анкета обновлена!", reply_markup=build_admin_panel())


@router.message(AdminEdit.editing_decline)
async def save_decline(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    settings_data["texts"]["decline_text"] = message.text or ""
    persist_all()
    await state.clear()
    await message.answer("Отказ обновлён!", reply_markup=build_admin_panel())


# =========================
# MAIN SCENARIO
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


@router.message(Onboarding.waiting_for_age, F.text)
async def process_age(message: Message, state: FSMContext):
    if not bot_is_running_for_welcome():
        await message.reply(bot_is_off_message())
        await state.clear()
        return

    if await maybe_ban_on_suspicious_links(message):
        await state.clear()
        return

    text = message.text or ""
    match = re.search(r"\d+", text.lower())
    if not match:
        await typed_delay(float(settings_data.get("reply_delay", 1)))
        await message.reply("Не совсем понял цифру. Напиши, пожалуйста, возраст числом 😊")
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

    if is_mode_1():
        await state.clear()
        return

    await typed_delay(float(settings_data.get("reply_delay", 1)))
    await message.reply(get_texts()["consent_text"].format(age=age))
    await state.set_state(Onboarding.waiting_for_consent)


@router.message(Onboarding.waiting_for_consent, F.text)
async def process_consent(message: Message, state: FSMContext):
    if not bot_is_running_for_welcome():
        await message.reply(bot_is_off_message())
        await state.clear()
        return

    if await maybe_ban_on_suspicious_links(message):
        await state.clear()
        return

    text = (message.text or "").lower().strip()

    positive_words = [
        "да", "давай", "ок", "окей", "хочу", "+", "конечно", "угу", "ага", "yes", "ладно",
        "готов", "готова", "го", "погнали", "ну давай", "давай попробуем", "ладно давай",
        "попробую", "почему бы и нет, даай", "почему бы и нет, давай", "вай нот", "гоу",
        "летс",
    ]
    is_agreed = any(word in text.split() for word in positive_words) or text in positive_words

    await typed_delay(float(settings_data.get("reply_delay", 1)))

    if is_agreed:
        await message.reply(get_texts()["questionnaire_text"])
        await state.set_state(Onboarding.waiting_for_questionnaire)
    else:
        await message.reply(get_texts()["decline_text"])
        await state.clear()


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

    notify_ids = get_notify_admins()
    username_display = (
        f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    )

    for admin_id in notify_ids:
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


@router.message(Command("sv"))
async def send_questionnaire_cmd(message: Message):
    if not bot_is_running_for_questionnaire():
        await message.reply(bot_is_off_message())
        return
    await typed_delay(float(settings_data.get("reply_delay", 1)))
    await message.reply(get_texts()["questionnaire_text"])


# =========================
# RUN
# =========================
async def main():
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
