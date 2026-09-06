import os
import re
import hashlib
import secrets
import asyncio
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
# ПРОВЕРКА ENV
# ============================================================

required_env = {
    "BOT_TOKEN": BOT_TOKEN,
    "RENDER_URL": RENDER_URL,
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_SERVICE_KEY": SUPABASE_SERVICE_KEY,
}

missing_env = [
    name
    for name, value in required_env.items()
    if not value
]

if missing_env:
    raise RuntimeError(
        "Не заданы переменные окружения: "
        + ", ".join(missing_env)
    )


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
# TELEGRAM
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
            .eq(
                "password_hash",
                hash_password(password)
            )
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

    result = (
        supabase
        .table("bot_users")
        .select("authorized")
        .eq("telegram_id", telegram_id)
        .execute()
    )

    if not result.data:
        return False

    return bool(
        result.data[0].get("authorized", False)
    )


def use_password(password, telegram_id):

    password_hash = hash_password(
        password.strip()
    )

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

    now = datetime.now(
        timezone.utc
    ).isoformat()

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
# SKYSMART LINK
# ============================================================

def extract_room_name(text):

    if not text:
        return None

    text = text.strip()

    match = re.search(
        r"https?://edu\.skysmart\.ru/student/([^/?#\s]+)",
        text,
        re.IGNORECASE
    )

    if not match:
        return None

    room = match.group(1)

    room = room.rstrip(
        ".,;:!?)]}>\"'"
    )

    return room


# ============================================================
# ЗАПРОС К SKYSMART API
# ============================================================

def get_skysmart_answers(room_name):

    payload = {
        "roomName": room_name
    }

    logger.info(
        "========================================"
    )

    logger.info(
        "Skysmart API request"
    )

    logger.info(
        "roomName: %s",
        room_name
    )

    logger.info(
        "URL: %s",
        SKYSMART_API_URL
    )

    try:

        response = requests.post(
            SKYSMART_API_URL,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0"
            },
            timeout=30
        )

    except requests.Timeout:

        logger.error(
            "Skysmart API TIMEOUT"
        )

        raise

    except requests.RequestException as e:

        logger.error(
            "Skysmart API connection error: %s",
            e
        )

        raise

    logger.info(
        "Skysmart API HTTP status: %s",
        response.status_code
    )

    logger.info(
        "Skysmart API response:"
    )

    logger.info(
        "%s",
        response.text[:5000]
    )

    logger.info(
        "========================================"
    )

    if not response.ok:

        raise requests.HTTPError(
            f"HTTP {response.status_code}: "
            f"{response.text[:2000]}",
            response=response
        )

    try:

        return response.json()

    except ValueError:

        logger.error(
            "Skysmart API returned invalid JSON"
        )

        raise ValueError(
            "Skysmart API вернул не JSON"
        )


# ============================================================
# СОЗДАНИЕ all_tasks_answers
# ============================================================

def build_all_tasks_answers(data):

    """
    Получает ответ API и создаёт:

    all_tasks_answers = [
        ["π/2", "πm", "πn"],
        ["π/3", "πk", "2πn"],
        ["5", "10", "15"]
    ]

    Каждое задание = отдельный список.

    Содержимое строк ответов не очищается.
    HTML/LaTeX не преобразуется.
    """

    if not isinstance(data, list):

        raise ValueError(
            "API вернул не список."
        )

    if len(data) == 0:

        raise ValueError(
            "API вернул пустой список."
        )

    tasks = data[0]

    if not isinstance(tasks, list):

        raise ValueError(
            "data[0] не является списком заданий."
        )

    all_tasks_answers = []

    for task_index, task in enumerate(tasks, start=1):

        if not isinstance(task, dict):

            logger.warning(
                "Задание %s имеет неожиданный тип: %s",
                task_index,
                type(task).__name__
            )

            all_tasks_answers.append([])

            continue

        # ----------------------------------------------------
        # Основное поле API
        # ----------------------------------------------------

        answers = task.get("answers")

        # ----------------------------------------------------
        # Если answers отсутствует,
        # проверяем альтернативные названия.
        # ----------------------------------------------------

        if answers is None:

            for key in (
                "answer",
                "correct_answer",
                "correctAnswer",
                "results",
                "solution"
            ):

                if key in task:

                    answers = task[key]

                    logger.info(
                        "Задание %s: использовано поле '%s'",
                        task_index,
                        key
                    )

                    break

        # ----------------------------------------------------
        # Ничего нет
        # ----------------------------------------------------

        if answers is None:

            answers = []

        # ----------------------------------------------------
        # Один строковый ответ
        # ----------------------------------------------------

        if isinstance(answers, str):

            answers = [
                answers
            ]

        # ----------------------------------------------------
        # Уже список
        # ----------------------------------------------------

        elif isinstance(answers, list):

            # Не изменяем содержимое.
            answers = list(answers)

        # ----------------------------------------------------
        # Другой тип
        # ----------------------------------------------------

        else:

            answers = [
                answers
            ]

        all_tasks_answers.append(
            answers
        )

    logger.info(
        "Получен all_tasks_answers: %s",
        all_tasks_answers
    )

    return all_tasks_answers


# ============================================================
# ФОРМАТ ВЫВОДА
# ============================================================

def format_all_tasks_answers(all_tasks_answers):

    """
    Вывод:

    [
        ['π/2', 'πm', 'πn'],
        ['π/3', 'πk'],
        ['5', '10', '15']
    ]

    Если нужен именно JSON с двойными кавычками,
    можно заменить repr() на json.dumps().
    """

    return repr(
        all_tasks_answers
    )


# ============================================================
# ДЛИННЫЕ СООБЩЕНИЯ
# ============================================================

def split_message(
    text,
    max_length=MAX_MESSAGE_LENGTH
):

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

        chunks.append(
            text[:cut]
        )

        text = text[cut:]

        if text.startswith("\n"):
            text = text[1:]

    if text:
        chunks.append(text)

    return chunks


async def send_long_message(
    message,
    text
):

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

    if not update.effective_user:
        return

    telegram_id = update.effective_user.id

    create_user_if_needed(
        telegram_id
    )

    if is_authorized(
        telegram_id
    ):

        await update.message.reply_text(
            "✅ Вы авторизованы.\n\n"
            "Отправьте ссылку на задание Skysmart."
        )

        return

    await update.message.reply_text(
        "🔐 Для использования бота "
        "введите пароль."
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
        f"Ваш Telegram ID:\n"
        f"{update.effective_user.id}"
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
            "❌ У вас нет доступа "
            "к этой команде."
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
# ОБЫЧНЫЕ СООБЩЕНИЯ
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.effective_user:
        return

    text = update.message.text

    if not text:
        return

    text = text.strip()

    telegram_id = (
        update.effective_user.id
    )

    # --------------------------------------------------------
    # Пользователь
    # --------------------------------------------------------

    create_user_if_needed(
        telegram_id
    )

    # --------------------------------------------------------
    # Авторизация
    # --------------------------------------------------------

    if not is_authorized(
        telegram_id
    ):

        success = use_password(
            text,
            telegram_id
        )

        if success:

            await update.message.reply_text(
                "✅ Пароль принят!\n\n"
                "Теперь отправьте ссылку "
                "на задание Skysmart."
            )

        else:

            await update.message.reply_text(
                "❌ Неверный или уже "
                "использованный пароль."
            )

        return

    # --------------------------------------------------------
    # Ссылка Skysmart
    # --------------------------------------------------------

    room_name = extract_room_name(
        text
    )

    if not room_name:

        await update.message.reply_text(
            "❗ Отправьте ссылку "
            "на задание Skysmart.\n\n"
            "Например:\n"
            "https://edu.skysmart.ru/student/..."
        )

        return

    # --------------------------------------------------------
    # Сообщение обработки
    # --------------------------------------------------------

    processing_message = (
        await update.message.reply_text(
            "⏳ Получаю задания и ответы..."
        )
    )

    try:

        # ----------------------------------------------------
        # Получаем API
        # ----------------------------------------------------

        data = await asyncio.to_thread(
            get_skysmart_answers,
            room_name
        )

        # ----------------------------------------------------
        # Создаём all_tasks_answers
        # ----------------------------------------------------

        all_tasks_answers = (
            build_all_tasks_answers(
                data
            )
        )

        # ----------------------------------------------------
        # Форматируем
        # ----------------------------------------------------

        result_text = (
            format_all_tasks_answers(
                all_tasks_answers
            )
        )

        # ----------------------------------------------------
        # Удаляем "Получаю..."
        # ----------------------------------------------------

        try:

            await processing_message.delete()

        except Exception:

            pass

        # ----------------------------------------------------
        # Отправляем результат
        # ----------------------------------------------------

        await send_long_message(
            update.message,
            result_text
        )

    # ========================================================
    # TIMEOUT
    # ========================================================

    except requests.Timeout:

        try:
            await processing_message.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "⏱ Skysmart API не ответил "
            "за 30 секунд."
        )

    # ========================================================
    # HTTP ERROR
    # ========================================================

    except requests.HTTPError as e:

        logger.exception(
            "HTTP ошибка Skysmart API: %s",
            e
        )

        try:
            await processing_message.delete()
        except Exception:
            pass

        status_code = None

        if e.response is not None:

            status_code = (
                e.response.status_code
            )

        await update.message.reply_text(
            f"❌ Skysmart API вернул "
            f"ошибку HTTP {status_code}.\n\n"
            "Подробности находятся "
            "в логах Render."
        )

    # ========================================================
    # ОШИБКА СОЕДИНЕНИЯ
    # ========================================================

    except requests.RequestException as e:

        logger.exception(
            "Ошибка соединения "
            "со Skysmart API: %s",
            e
        )

        try:
            await processing_message.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Ошибка соединения "
            "с сервером Skysmart API."
        )

    # ========================================================
    # ОШИБКА СТРУКТУРЫ API
    # ========================================================

    except ValueError as e:

        logger.exception(
            "Ошибка структуры ответа "
            "Skysmart API: %s",
            e
        )

        try:
            await processing_message.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Skysmart API вернул "
            "неожиданный формат данных.\n\n"
            "Подробности находятся "
            "в логах Render."
        )

    # ========================================================
    # ЛЮБАЯ ДРУГАЯ ОШИБКА
    # ========================================================

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
            "❌ Произошла ошибка "
            "при обработке задания."
        )


# ============================================================
# HANDLERS
# ============================================================

application.add_handler(
    CommandHandler(
        "start",
        start_command
    )
)

application.add_handler(
    CommandHandler(
        "id",
        id_command
    )
)

application.add_handler(
    CommandHandler(
        "keys",
        keys_command
    )
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
        "Webhook установлен:"
    )

    logger.info(
        "%s",
        webhook_url
    )


# ============================================================
# STARTUP
# ============================================================

async def startup():

    logger.info(
        "========================================"
    )

    logger.info(
        "Запуск Telegram бота..."
    )

    logger.info(
        "========================================"
    )

    initialize_passwords()

    passwords = get_unused_passwords()

    logger.info(
        "Пароли: свободных %s",
        len(passwords)
    )

    await application.initialize()

    await application.start()

    await set_webhook()

    logger.info(
        "Бот успешно запущен!"
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def shutdown():

    logger.info(
        "Остановка Telegram бота..."
    )

    try:
        await application.stop()
    except Exception:
        pass

    try:
        await application.shutdown()
    except Exception:
        pass


# ============================================================
# HTTP
# ============================================================

async def home(
    request: Request
):

    return PlainTextResponse(
        "Telegram bot is running."
    )


async def health(
    request: Request
):

    return JSONResponse({
        "status": "ok"
    })


async def telegram_webhook(
    request: Request
):

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

    on_startup=[
        startup
    ],

    on_shutdown=[
        shutdown
    ],
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
