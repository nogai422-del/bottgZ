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
from aiogram.types import Message
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
# LOGGING
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


def load_settings() -> dict:
    _ensure_files()
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


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


# =========================
# HELPERS
# =========================
async def typed_delay(delay: float):
    if delay and delay > 0:
        await asyncio.sleep(delay)


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


# =========================
# DETECT + BAN
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


def is_refusal(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()

    refusal_phrases = [
        "не хочу", "не хочy", "не хочу называть", "не буду", "не назову",
        "я не хочу", "я не буду", "отказываюсь",
        "нет", "не согласен", "не согласна", "не надо", "убери",
        "не хочу заполнять", "не буду заполнять", "не хочу анкету",
        "не хочу называть возраст", "не скажу возраст", "не скажу",
        "я против", "против", "отказ", "не хочу отвечать", "не буду отвечать",
    ]
    for p in refusal_phrases:
        if p in t:
            return True

    short_refusals = {"нет", "не хочу", "не буду", "против", "отказ"}
    return t in short_refusals


def words_to_int_ru(text: str) -> int | None:
    if not text:
        return None

    t = text.strip().lower()
    t = re.sub(r"[^\w\s-]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()

    m = re.search(r"\d{1,3}", t)
    if m:
        return int(m.group(0))

    units = {
        "ноль": 0, "один": 1, "одна": 1, "одно": 1,
        "два": 2, "две": 2,
        "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
        "семь": 7, "восемь": 8, "девять": 9,
    }
    teens = {
        "десять": 10, "одиннадцать": 11, "двенадцать": 12,
        "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
        "шестнадцать": 16, "семнадцать": 17,
    }
    tens = {
        "двадцать": 20, "тридцать": 30, "сорок": 40,
        "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
    }

    tokens = t.split()
    if len(tokens) == 1:
        tok = tokens[0]
        if tok in units:
            return units[tok]
        if tok in teens:
            return teens[tok]
        if tok in tens:
            return tens[tok]
        return None

    joined = " ".join(tokens)
    if joined in teens:
        return teens[joined]

    tens_val = None
    for tok in tokens:
        if tok in tens:
            tens_val = tens[tok]
            break

    if tens_val is not None:
        total = tens_val
        for tok in tokens:
            if tok in units:
                total += units[tok]
                return total
        return total

    return None


async def safe_delete(chat_id: int, message_id: int, tag: str = ""):
    if not message_id:
        return False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        await botlog(f"DELETE ok {tag} chat_id={chat_id} message_id={message_id}")
        return True
    except Exception as e:
        await botlog(
            f"DELETE FAIL {tag} chat_id={chat_id} message_id={message_id} err={repr(e)}"
        )
        return False


def message_link(chat_id: int, message_id: int) -> str:
    return f"chat_id={chat_id} message_id={message_id}"


async def do_ban(message: Message, reason: str):
    chat_id = message.chat.id
    user_id = message.from_user.id

    await botlog(f"BAN start chat_id={chat_id} user_id={user_id} reason={reason}")

    # delete the current message (best-effort)
    try:
        await message.delete()
    except Exception:
        pass

    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        await botlog(f"BAN failed chat_id={chat_id} user_id={user_id} err={repr(e)}")
        return

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


async def ban_with_cleanup(message: Message, reason: str, state: FSMContext):
    chat_id = message.chat.id
    data = await state.get_data()

    user_ids = data.get("user_message_ids", []) or []
    bot_ids = data.get("bot_message_ids", []) or []

    if message.message_id not in user_ids:
        user_ids.append(message.message_id)

    delete_ids = list(set(user_ids + bot_ids))

    joined_mid = data.get("joined_message_id")
    welcome_mid = data.get("welcome_message_id")

    if joined_mid:
        delete_ids.append(joined_mid)
    if welcome_mid:
        delete_ids.append(welcome_mid)

    # system-ish first
    if joined_mid:
        await safe_delete(chat_id, joined_mid, tag="joined")
    if welcome_mid:
        await safe_delete(chat_id, welcome_mid, tag="welcome")

    for mid in set(delete_ids):
        if mid in (joined_mid, welcome_mid):
            continue
        await safe_delete(chat_id, mid, tag="collected")

    await do_ban(message, reason)
    await state.clear()


# =========================
# SYSTEM MESSAGES: вход / исключение
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

        user_state = FSMContext(
            storage=dp.storage,
            key=StorageKey(
                bot_id=bot.id,
                chat_id=message.chat.id,
                user_id=new_member.id,
            ),
        )

        await user_state.set_state(Onboarding.waiting_for_age)

        welcome_msg = await message.reply(welcome_text)

        await user_state.update_data(
            joined_message_id=message.message_id,      # системное сообщение о входе (best-effort)
            welcome_message_id=welcome_msg.message_id,  # ответ бота
            user_message_ids=[],
            bot_message_ids=[welcome_msg.message_id],
        )

        await botlog(f"WELCOME sent user_id={new_member.id} chat_id={message.chat.id}")


# ✅ ВАЖНО: удаляем системное сообщение об исключении (left_chat_member)
@router.message(F.left_chat_member)
async def left_chat_member_handler(message: Message):
    # message.message_id у системного апдейта может не удаляться всегда,
    # но мы делаем best-effort и логируем.
    chat_id = message.chat.id

    # В aiogram v3: message.left_chat_member может быть User или list[User]
    left_obj = getattr(message, "left_chat_member", None)
    user_ids = []

    if left_obj is None:
        user_ids = []
    elif isinstance(left_obj, list):
        user_ids = [u.id for u in left_obj if u and getattr(u, "id", None)]
    else:
        # одиночный объект
        uid = getattr(left_obj, "id", None)
        user_ids = [uid] if uid else []

    tag = f"left({','.join(map(str, user_ids))})" if user_ids else "left"
    await safe_delete(chat_id, message.message_id, tag=tag)


# =========================
# USER SCENARIO
# =========================
@router.message(Onboarding.waiting_for_age, F.text)
async def process_age(message: Message, state: FSMContext):
    if not bot_is_running_for_welcome():
        await message.reply(bot_is_off_message())
        await state.clear()
        return

    if await maybe_ban_on_suspicious_links(message):
        await state.clear()
        return

    data = await state.get_data()
    user_message_ids = data.get("user_message_ids", []) or []
    if message.message_id not in user_message_ids:
        user_message_ids.append(message.message_id)
        await state.update_data(user_message_ids=user_message_ids)

    if is_refusal(message.text or ""):
        await ban_with_cleanup(message, "Отказ назвать возраст", state)
        return

    text = (message.text or "").strip()

    age = None
    m = re.search(r"\d{1,3}", text)
    if m:
        age = int(m.group(0))
    else:
        age = words_to_int_ru(text)

    if age is None:
        # НЕ баним: просто уведомляем админов
        await botlog(f"AGE parse failed user_id={message.from_user.id} chat_id={message.chat.id}")

        profile_link = f'<a href="tg://user?id={message.from_user.id}">профиль</a>'
        link_info = message_link(message.chat.id, message.message_id)
        snippet = (message.text or "")[:400]

        for admin_id in get_notify_admins():
            try:
                await bot.send_message(
                    admin_id,
                    "⚠️ Не понял возраст (не распознал)\n\n"
                    f"Пользователь: {profile_link}\n"
                    f"ID: <code>{message.from_user.id}</code>\n"
                    f"Чат: <code>{message.chat.id}</code>\n"
                    f"Сообщение: <code>{link_info}</code>\n\n"
                    f"Текст: <code>{snippet}</code>",
                )
            except Exception:
                continue
        return

    if age < 18:
        await ban_with_cleanup(message, f"Возраст меньше 18: {age}", state)
        return

    if age >= 70:
        await ban_with_cleanup(message, f"Возраст 70+ : {age}", state)
        return

    if int(settings_data.get("mode", 2)) == 1:
        await state.clear()
        return

    await typed_delay(float(settings_data.get("reply_delay", 1)))

    consent_msg = await message.reply(get_texts()["consent_text"].format(age=age))

    data = await state.get_data()
    bot_message_ids = data.get("bot_message_ids", []) or []
    if consent_msg.message_id not in bot_message_ids:
        bot_message_ids.append(consent_msg.message_id)
        await state.update_data(bot_message_ids=bot_message_ids)

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

    data = await state.get_data()
    user_message_ids = data.get("user_message_ids", []) or []
    if message.message_id not in user_message_ids:
        user_message_ids.append(message.message_id)
        await state.update_data(user_message_ids=user_message_ids)

    text = (message.text or "").lower().strip()
    positive_words = [
        "да", "давай", "ок", "окей", "хочу", "+", "конечно", "угу", "ага", "yes",
        "ладно", "готов", "готова", "го", "погнали", "ну давай", "давай попробуем",
        "ладно давай", "попробую", "вай нот", "гоу", "летс",
    ]
    words = set(text.split())
    is_agreed = any(w in words for w in positive_words) or text in positive_words

    await typed_delay(float(settings_data.get("reply_delay", 1)))

    if is_agreed:
        questionnaire_msg = await message.reply(get_texts()["questionnaire_text"])
        data = await state.get_data()
        bot_message_ids = data.get("bot_message_ids", []) or []
        if questionnaire_msg.message_id not in bot_message_ids:
            bot_message_ids.append(questionnaire_msg.message_id)
            await state.update_data(bot_message_ids=bot_message_ids)

        await state.set_state(Onboarding.waiting_for_questionnaire)
    else:
        decline_msg = await message.reply(get_texts()["decline_text"])
        data = await state.get_data()
        bot_message_ids = data.get("bot_message_ids", []) or []
        if decline_msg.message_id not in bot_message_ids:
            bot_message_ids.append(decline_msg.message_id)
            await state.update_data(bot_message_ids=bot_message_ids)

        await state.clear()


@router.message(Onboarding.waiting_for_questionnaire, F.text)
async def process_questionnaire_done(message: Message, state: FSMContext):
    if await maybe_ban_on_suspicious_links(message):
        await state.clear()
        return

    data = await state.get_data()
    user_message_ids = data.get("user_message_ids", []) or []
    if message.message_id not in user_message_ids:
        user_message_ids.append(message.message_id)
        await state.update_data(user_message_ids=user_message_ids)

    if is_refusal(message.text or ""):
        await ban_with_cleanup(message, "Отказ заполнять анкету", state)
        return

    username_display = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else message.from_user.first_name
    )

    await botlog(f"QUESTIONNAIRE accepted user_id={message.from_user.id}")

    for admin_id in get_notify_admins():
        try:
            await bot.send_message(
                admin_id,
                "Анкета заполнена\n\n"
                f"Кто: {username_display} (ID: <code>{message.from_user.id}</code>)\n"
                f"Чат: {message.chat.title}\n\n"
                "Действие: пользователь отправил текст анкеты",
            )
        except Exception:
            continue

    ok_msg = await message.reply("Отлично! Анкета принята.")

    data = await state.get_data()
    bot_message_ids = data.get("bot_message_ids", []) or []
    if ok_msg.message_id not in bot_message_ids:
        bot_message_ids.append(ok_msg.message_id)
        await state.update_data(bot_message_ids=bot_message_ids)

    await state.clear()


@router.message(Command("sv"))
async def send_questionnaire_cmd(message: Message):
    if not bot_is_running_for_welcome():
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
