import os
import re
import hashlib
import secrets
import asyncio
import json
import logging

import requests
import uvicorn

from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from supabase import create_client, Client


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

SKYSMART_API_URL = "https://skysmart-answers.vercel.app/get_answers/"

WEBHOOK_PATH = "/telegram"

MAX_MESSAGE_LENGTH = 3900


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# SUPABASE
# ============================================================

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

application = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


# ============================================================
# ПАРОЛИ
# ============================================================

PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_password():
    """
    Генерирует пароль вида XXXX-XXXX.
    """

    part1 = "".join(
        secrets.choice(PASSWORD_ALPHABET)
        for _ in range(4)
    )

    part2 = "".join(
        secrets.choice(PASSWORD_ALPHABET)
        for _ in range(4)
    )

    return f"{part1}-{part2}"


def hash_password(password):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def initialize_passwords():
    """
    Поддерживает минимум 10 свободных паролей.
    """

    result = (
        supabase
        .table("bot_passwords")
        .select("id")
        .eq("used", False)
        .execute()
    )

    free_count = len(result.data or [])

    while free_count < 10:

        password = generate_password()

        exists = (
            supabase
            .table("bot_passwords")
            .select("id")
            .eq("password_hash", hash_password(password))
            .execute()
        )

        if exists.data:
            continue

        supabase.table("bot_passwords").insert({
            "password_hash": hash_password(password),
            "password_text": password,
            "used": False,
            "used_by": None,
            "used_at": None,
        }).execute()

        free_count += 1


def create_user_if_needed(telegram_id):
    """
    Создаёт пользователя в bot_users, если его ещё нет.
    """

    result = (
        supabase
        .table("bot_users")
        .select("telegram_id")
        .eq("telegram_id", telegram_id)
        .execute()
    )

    if not result.data:
        supabase.table("bot_users").insert({
            "telegram_id": telegram_id,
            "authorized": False,
        }).execute()


def is_authorized(telegram_id):
    """
    Проверяет авторизацию пользователя.
    """

    result = (
        supabase
        .table("bot_users")
        .select("authorized")
        .eq("telegram_id", telegram_id)
        .execute()
    )

    if not result.data:
        return False

    return bool(result.data[0].get("authorized", False))


def use_password(password, telegram_id):
    """
    Использует пароль и авторизует пользователя.
    """

    password_hash = hash_password(password.strip())

    result = (
        supabase
        .table("bot_passwords")
        .select("*")
        .eq("password_hash", password_hash)
        .eq("used", False)
        .limit(1)
        .execute()
    )

    if not result.data:
        return False

    password_row = result.data[0]

    now = datetime.now(timezone.utc).isoformat()

    supabase.table("bot_passwords").update({
        "used": True,
        "used_by": telegram_id,
        "used_at": now,
    }).eq(
        "id",
        password_row["id"]
    ).execute()

    supabase.table("bot_users").upsert({
        "telegram_id": telegram_id,
        "authorized": True,
    }).execute()

    return True


def get_unused_passwords():
    """
    Возвращает свободные пароли.
    """

    result = (
        supabase
        .table("bot_passwords")
        .select("password_text")
        .eq("used", False)
        .order("id")
        .execute()
    )

    return [
        row["password_text"]
        for row in (result.data or [])
    ]


# ============================================================
# SKYSМART
# ============================================================

def extract_room_name(text):
    """
    Извлекает room из ссылки:

    https://edu.skysmart.ru/student/kavorubamu

    Возвращает:

    kavorubamu
    """

    if not text:
        return None

    text = text.strip()

    match = re.search(
        r"https?://edu\.skysmart\.ru/student/([^/?#\s]+)",
        text,
        re.IGNORECASE
    )

    if match:
        room = match.group(1)

        # Убираем только пунктуацию,
        # которая могла случайно оказаться после ссылки.
        room = room.rstrip(".,;:!?)]}>\"'")

        return room

    return None


def get_skysmart_answers(room_name):
    """
    Получает данные от Skysmart API.
    """

    response = requests.post(
        SKYSMART_API_URL,
        json={
            "roomName": room_name
        },
        timeout=30
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# ФОРМИРОВАНИЕ ALL_TASKS_ANSWERS
# ============================================================

def build_all_tasks_answers(data):
    """
    Преобразует ответ API в структуру:

    all_tasks_answers = [
        ["ответ 1", "ответ 2", "ответ 3"],
        ["ответ 1", "ответ 2"],
        ["ответ 1", "ответ 2", "ответ 3"]
    ]

    То есть:

    all_tasks_answers[0] -> ответы задания 1
    all_tasks_answers[1] -> ответы задания 2
    all_tasks_answers[2] -> ответы задания 3

    ВАЖНО:

    Содержимое ответов НЕ очищается.
    HTML НЕ преобразуется.
    LaTeX НЕ преобразуется.
    Символы НЕ заменяются.
    Строки не декодируются.

    Берём answers непосредственно из API.
    """

    if not isinstance(data, list):
        raise ValueError("Некорректный ответ API: ожидался список.")

    if len(data) == 0:
        raise ValueError("API вернул пустой ответ.")

    tasks = data[0]

    if not isinstance(tasks, list):
        raise ValueError(
            "Некорректная структура API: data[0] должен быть списком заданий."
        )

    all_tasks_answers = []

    for task in tasks:

        if not isinstance(task, dict):
            continue

        # ----------------------------------------------------
        # Основной вариант API:
        #
        # task["answers"] = [...]
        # ----------------------------------------------------

        answers = task.get("answers")

        if answers is None:
            answers = []

        # Если API почему-то прислал одну строку,
        # превращаем её в список из одного элемента.
        #
        # Сам текст при этом НЕ изменяем.
        if isinstance(answers, str):
            answers = [answers]

        elif isinstance(answers, list):
            # Копируем список без изменения содержимого.
            answers = list(answers)

        else:
            answers = [answers]

        all_tasks_answers.append(answers)

    return all_tasks_answers


def format_all_tasks_answers(all_tasks_answers):
    """
    Делает из структуры all_tasks_answers текст,
    который можно отправить пользователю.

    Используется repr(), чтобы Telegram получил именно
    Python-подобный вид вложенных списков.

    Например:

    [
        ['π/2', 'πm', 'πn'],
        ['π/3', 'πk'],
        ['5', '10', '15']
    ]
    """

    return repr(all_tasks_answers)


# ============================================================
# РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ
# ============================================================

def split_message(text, max_length=MAX_MESSAGE_LENGTH):

    if len(text) <= max_length:
        return [text]

    chunks = []

    while len(text) > max_length:

        cut = text.rfind(
            "\n",
            0,
            max_length
        )

        if cut <= 0:
            cut = max_length

        chunks.append(text[:cut])
        text = text[cut:]

        if text.startswith("\n"):
            text = text[1:]

    if text:
        chunks.append(text)

    return chunks


async def send_long_message(message, text):

    chunks = split_message(text)

    for chunk in chunks:

        await message.reply_text(
            chunk,
            parse_mode=None,
            disable_web_page_preview=True
        )


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    telegram_id = user.id

    create_user_if_needed(telegram_id)

    if is_authorized(telegram_id):

        await update.message.reply_text(
            "✅ Вы авторизованы.\n\n"
            "Отправьте ссылку на задание Skysmart."
        )

        return

    await update.message.reply_text(
        "🔐 Для использования бота введите пароль."
    )


# ============================================================
# /ID
# ============================================================

async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    await update.message.reply_text(
        f"Ваш Telegram ID:\n{update.effective_user.id}"
    )


# ============================================================
# /KEYS
# ============================================================

async def keys_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if update.effective_user.id != OWNER_ID:

        await update.message.reply_text(
            "❌ У вас нет доступа к этой команде."
        )

        return

    passwords = get_unused_passwords()

    if not passwords:

        await update.message.reply_text(
            "Свободных паролей нет."
        )

        return

    text = "\n".join(
        f"`{password}`"
        for password in passwords
    )

    await update.message.reply_text(
        f"🔑 Свободные пароли:\n\n{text}",
        parse_mode="Markdown"
    )


# ============================================================
# ОБРАБОТКА СООБЩЕНИЙ
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    telegram_id = update.effective_user.id

    text = update.message.text

    if not text:
        return

    text = text.strip()

    create_user_if_needed(telegram_id)

    # ========================================================
    # ПРОВЕРКА АВТОРИЗАЦИИ
    # ========================================================

    if not is_authorized(telegram_id):

        success = use_password(
            text,
            telegram_id
        )

        if success:

            await update.message.reply_text(
                "✅ Пароль принят!\n\n"
                "Теперь отправьте ссылку на задание Skysmart."
            )

        else:

            await update.message.reply_text(
                "❌ Неверный или уже использованный пароль."
            )

        return

    # ========================================================
    # ИЗВЛЕКАЕМ ROOM
    # ========================================================

    room_name = extract_room_name(text)

    if not room_name:

        await update.message.reply_text(
            "❗ Отправьте ссылку на задание Skysmart.\n\n"
            "Например:\n"
            "https://edu.skysmart.ru/student/..."
        )

        return

    # ========================================================
    # ПОЛУЧАЕМ ОТВЕТЫ
    # ========================================================

    processing_message = await update.message.reply_text(
        "⏳ Получаю задания и ответы..."
    )

    try:

        data = await asyncio.to_thread(
            get_skysmart_answers,
            room_name
        )

        # ====================================================
        # СОЗДАЁМ:
        #
        # all_tasks_answers = [
        #     [...],
        #     [...],
        #     [...]
        # ]
        # ====================================================

        all_tasks_answers = build_all_tasks_answers(data)

        # ====================================================
        # ПРЕОБРАЗУЕМ В ТЕКСТ
        # ====================================================

        result_text = format_all_tasks_answers(
            all_tasks_answers
        )

        try:
            await processing_message.delete()
        except Exception:
            pass

        # ====================================================
        # ОТПРАВЛЯЕМ РЕЗУЛЬТАТ
        #
        # parse_mode=None !!!
        #
        # Telegram НЕ должен пытаться обрабатывать HTML,
        # Markdown или LaTeX.
        # ====================================================

        await send_long_message(
            update.message,
            result_text
        )

    except requests.Timeout:

        try:
            await processing_message.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "⏱ Сервер Skysmart слишком долго отвечает. "
            "Попробуйте ещё раз."
        )

    except requests.RequestException as e:

        logger.exception(
            "Ошибка запроса Skysmart: %s",
            e
        )

        try:
            await processing_message.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Не удалось получить ответы от Skysmart."
        )

    except Exception as e:

        logger.exception(
            "Ошибка обработки Skysmart: %s",
            e
        )

        try:
            await processing_message.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Произошла ошибка при обработке задания."
        )


# ============================================================
# HANDLERS
# ============================================================

application.add_handler(
    CommandHandler("start", start_command)
)

application.add_handler(
    CommandHandler("id", id_command)
)

application.add_handler(
    CommandHandler("keys", keys_command)
)

application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        message_handler
    )
)


# ============================================================
# WEBHOOK
# ============================================================

async def set_webhook():

    webhook_url = (
        RENDER_URL.rstrip("/")
        + WEBHOOK_PATH
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    logger.info(
        "Webhook установлен:\n%s",
        webhook_url
    )


# ============================================================
# STARTUP
# ============================================================

async def startup():

    logger.info("========================================")
    logger.info("Запуск Telegram бота...")
    logger.info("========================================")

    initialize_passwords()

    passwords = get_unused_passwords()

    logger.info(
        "Пароли: свободных %s",
        len(passwords)
    )

    await application.initialize()

    await application.start()

    await set_webhook()

    logger.info("Бот успешно запущен!")


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown():

    logger.info("Остановка Telegram бота...")

    try:
        await application.stop()
    except Exception:
        pass

    try:
        await application.shutdown()
    except Exception:
        pass


# ============================================================
# HTTP ROUTES
# ============================================================

async def home(request: Request):

    return PlainTextResponse(
        "Telegram bot is running."
    )


async def health(request: Request):

    return JSONResponse({
        "status": "ok"
    })


async def telegram_webhook(request: Request):

    try:

        body = await request.json()

        update = Update.de_json(
            body,
            application.bot
        )

        await application.process_update(
            update
        )

        return JSONResponse({
            "ok": True
        })

    except Exception as e:

        logger.exception(
            "Webhook error: %s",
            e
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e)
            },
            status_code=500
        )


# ============================================================
# STARLETTE
# ============================================================

app = Starlette(
    routes=[
        Route(
            "/",
            home,
            methods=["GET", "HEAD"]
        ),
        Route(
            "/health",
            health,
            methods=["GET"]
        ),
        Route(
            WEBHOOK_PATH,
            telegram_webhook,
            methods=["POST"]
        ),
    ],
    on_startup=[startup],
    on_shutdown=[shutdown],
)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )
