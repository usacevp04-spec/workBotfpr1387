import os
import re
import json
import time
import hashlib
import secrets
import logging
import asyncio

import requests
import uvicorn

from datetime import datetime, timezone

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from supabase import create_client


BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
OWNER_ID = os.getenv("OWNER_ID")
PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = "/telegram"
SKYSMART_API_URL = "https://skysmart-answers.vercel.app/get_answers/"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("skysmart_bot")

required_env = {
    "BOT_TOKEN": BOT_TOKEN,
    "RENDER_URL": RENDER_URL,
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_SERVICE_KEY": SUPABASE_SERVICE_KEY,
    "OWNER_ID": OWNER_ID,
}

missing_env = [
    name for name, value in required_env.items()
    if not value
]

if missing_env:
    raise RuntimeError(
        "Не заданы переменные окружения: "
        + ", ".join(missing_env)
    )


supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)

user_states = {}

PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def password_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🆔 Мой ID")]],
        resize_keyboard=True,
    )


def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📚 Получить ответы")],
            [KeyboardButton("🆔 Мой ID")],
        ],
        resize_keyboard=True,
    )


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
    try:
        result = (
            supabase
            .table("bot_passwords")
            .select("id")
            .eq("used", False)
            .execute()
        )

        current_count = len(result.data or [])

        while current_count < 10:
            password = generate_password()
            password_hash = hash_password(password)

            try:
                (
                    supabase
                    .table("bot_passwords")
                    .insert(
                        {
                            "password_hash": password_hash,
                            "password_text": password,
                            "used": False,
                        }
                    )
                    .execute()
                )

                current_count += 1

            except Exception as e:
                logger.error(
                    "Ошибка создания пароля: %s",
                    e
                )

    except Exception as e:
        logger.error(
            "Ошибка initialize_passwords: %s",
            e
        )


def create_user_if_needed(telegram_id):
    try:
        result = (
            supabase
            .table("bot_users")
            .select("*")
            .eq("telegram_id", telegram_id)
            .execute()
        )

        if result.data:
            return result.data[0]

        result = (
            supabase
            .table("bot_users")
            .insert(
                {
                    "telegram_id": telegram_id,
                    "authorized": False,
                }
            )
            .execute()
        )

        if result.data:
            return result.data[0]

    except Exception as e:
        logger.error(
            "Ошибка create_user_if_needed: %s",
            e
        )

    return None


def is_authorized(telegram_id):
    try:
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
            result.data[0].get("authorized")
        )

    except Exception as e:
        logger.error(
            "Ошибка is_authorized: %s",
            e
        )

        return False


def use_password(password, telegram_id):
    password = password.strip().upper()
    password_hash = hash_password(password)

    try:
        result = (
            supabase
            .table("bot_passwords")
            .select("*")
            .eq("password_hash", password_hash)
            .eq("used", False)
            .execute()
        )

        if not result.data:
            return False

        password_row = result.data[0]
        password_id = password_row["id"]

        (
            supabase
            .table("bot_passwords")
            .update(
                {
                    "used": True,
                    "used_by": telegram_id,
                    "used_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }
            )
            .eq("id", password_id)
            .execute()
        )

        (
            supabase
            .table("bot_users")
            .upsert(
                {
                    "telegram_id": telegram_id,
                    "authorized": True,
                }
            )
            .execute()
        )

        return True

    except Exception as e:
        logger.error(
            "Ошибка use_password: %s",
            e
        )

        return False


def make_password_list():
    try:
        result = (
            supabase
            .table("bot_passwords")
            .select("password_text")
            .eq("used", False)
            .order("id")
            .execute()
        )

        passwords = [
            row["password_text"]
            for row in (result.data or [])
        ]

        if not passwords:
            return "Свободных паролей нет."

        return "\n".join(
            f"`{password}`"
            for password in passwords
        )

    except Exception as e:
        logger.error(
            "Ошибка make_password_list: %s",
            e
        )

        return "Ошибка получения паролей."


def extract_room_name(text):
    text = text.strip()

    match = re.search(
        r"edu\.skysmart\.ru/student/([^/?#\s]+)",
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    if re.fullmatch(
        r"[A-Za-z0-9_-]+",
        text
    ):
        return text

    return None


def get_skysmart_answers(room_name):
    payload = {
        "roomName": room_name
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }

    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        logger.info(
            "Skysmart API: попытка %s/%s | room=%s",
            attempt,
            max_attempts,
            room_name,
        )

        try:
            response = requests.post(
                SKYSMART_API_URL,
                json=payload,
                headers=headers,
                timeout=60,
            )

            logger.info(
                "Skysmart API: HTTP %s",
                response.status_code,
            )

            if response.ok:
                try:
                    data = response.json()
                except ValueError:
                    logger.error(
                        "API вернул не JSON: %s",
                        response.text[:5000],
                    )

                    raise RuntimeError(
                        "Skysmart API вернул некорректный JSON."
                    )

                logger.info(
                    "Получен JSON от Skysmart API: %s",
                    json.dumps(
                        data,
                        ensure_ascii=False
                    )[:10000],
                )

                return data

            if response.status_code in (
                500,
                502,
                503,
                504,
            ):
                logger.warning(
                    "Skysmart API временно недоступен. HTTP %s",
                    response.status_code,
                )

                logger.warning(
                    "Ответ сервера: %s",
                    response.text[:5000],
                )

                if attempt < max_attempts:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                    continue

                raise requests.HTTPError(
                    f"Skysmart API HTTP "
                    f"{response.status_code}: "
                    f"{response.text[:5000]}"
                )

            logger.error(
                "Skysmart API вернул HTTP %s: %s",
                response.status_code,
                response.text[:5000],
            )

            response.raise_for_status()

        except requests.Timeout as e:
            logger.warning(
                "Timeout Skysmart API "
                "(попытка %s/%s): %s",
                attempt,
                max_attempts,
                e,
            )

            if attempt < max_attempts:
                time.sleep(2 ** attempt)
                continue

            raise RuntimeError(
                "Skysmart API не ответил за отведённое время."
            )

        except requests.ConnectionError as e:
            logger.warning(
                "Ошибка соединения с Skysmart API "
                "(попытка %s/%s): %s",
                attempt,
                max_attempts,
                e,
            )

            if attempt < max_attempts:
                time.sleep(2 ** attempt)
                continue

            raise RuntimeError(
                "Не удалось подключиться к Skysmart API."
            )

        except requests.HTTPError:
            raise

        except Exception as e:
            logger.exception(
                "Неожиданная ошибка при запросе к Skysmart API: %s",
                e,
            )

            raise

    raise RuntimeError(
        "Не удалось получить ответ от Skysmart API."
    )


def build_all_tasks_answers(data):
    if not isinstance(data, list):
        raise ValueError(
            "Ответ API должен быть списком."
        )

    if len(data) == 0:
        raise ValueError(
            "API вернул пустой список."
        )

    tasks = data[0]

    if not isinstance(tasks, list):
        raise ValueError(
            "data[0] должен содержать список заданий."
        )

    all_tasks_answers = [
        []
        for _ in range(len(tasks))
    ]

    for task in tasks:
        if not isinstance(task, dict):
            continue

        task_number = task.get("task_number")
        answers = task.get("answers", [])

        if not isinstance(task_number, int):
            continue

        index = task_number - 1

        if index < 0:
            continue

        while len(all_tasks_answers) <= index:
            all_tasks_answers.append([])

        if isinstance(answers, list):
            all_tasks_answers[index] = list(answers)

        elif isinstance(answers, str):
            all_tasks_answers[index] = [answers]

        else:
            all_tasks_answers[index] = [answers]

    logger.info(
        "ALL_TASKS_ANSWERS = %s",
        all_tasks_answers
    )

    return all_tasks_answers


def format_all_tasks_answers(all_tasks_answers):
    return json.dumps(
        all_tasks_answers,
        ensure_ascii=False,
        indent=2,
    )


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.effective_user:
        return

    telegram_id = update.effective_user.id

    create_user_if_needed(telegram_id)

    if is_authorized(telegram_id):
        user_states[telegram_id] = "waiting_link"

        await update.message.reply_text(
            "✅ Вы уже авторизованы.\n\n"
            "Отправьте ссылку на задание Skysmart:\n\n"
            "https://edu.skysmart.ru/student/ROOM",
            reply_markup=main_keyboard(),
        )

        return

    user_states[telegram_id] = "waiting_password"

    await update.message.reply_text(
        "🔐 Для использования бота введите пароль доступа.",
        reply_markup=password_keyboard(),
    )


async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.effective_user:
        return

    telegram_id = update.effective_user.id

    await update.message.reply_text(
        f"🆔 Ваш Telegram ID:\n\n{telegram_id}"
    )


async def keys_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.effective_user:
        return

    telegram_id = str(
        update.effective_user.id
    )

    if telegram_id != str(OWNER_ID):
        await update.message.reply_text(
            "❌ У вас нет доступа."
        )

        return

    passwords = make_password_list()

    await update.message.reply_text(
        "🔑 Свободные пароли:\n\n" + passwords
    )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.effective_user:
        return

    if not update.message:
        return

    telegram_id = update.effective_user.id

    text = (
        update.message.text or ""
    ).strip()

    if text == "🆔 Мой ID":
        await id_command(
            update,
            context
        )

        return

    if text == "📚 Получить ответы":
        user_states[telegram_id] = "waiting_link"

        await update.message.reply_text(
            "Отправьте ссылку на задание Skysmart."
        )

        return

    if not is_authorized(telegram_id):
        if user_states.get(telegram_id) == "waiting_password":
            if use_password(
                text,
                telegram_id
            ):
                user_states[telegram_id] = "waiting_link"

                await update.message.reply_text(
                    "✅ Пароль принят!\n\n"
                    "Теперь отправьте ссылку на задание Skysmart.",
                    reply_markup=main_keyboard(),
                )

            else:
                await update.message.reply_text(
                    "❌ Неверный или уже использованный пароль.\n\n"
                    "Попробуйте ещё раз."
                )

            return

        user_states[telegram_id] = "waiting_password"

        await update.message.reply_text(
            "🔐 Сначала введите пароль доступа."
        )

        return

    room_name = extract_room_name(text)

    if not room_name:
        await update.message.reply_text(
            "❌ Не удалось найти roomName.\n\n"
            "Отправьте ссылку вида:\n"
            "https://edu.skysmart.ru/student/ROOM"
        )

        return

    processing_message = await update.message.reply_text(
        "⏳ Получаю задания и ответы с Skysmart...",
        parse_mode=None,
    )

    try:
        data = await asyncio.to_thread(
            get_skysmart_answers,
            room_name
        )

        all_tasks_answers = build_all_tasks_answers(
            data
        )

        result_text = format_all_tasks_answers(
            all_tasks_answers
        )

        logger.info(
            "Готовый ALL_TASKS_ANSWERS:\n%s",
            result_text
        )

        try:
            await processing_message.delete()
        except Exception:
            pass

        max_length = 3900

        if len(result_text) <= max_length:
            await update.message.reply_text(
                result_text,
                parse_mode=None,
            )

        else:
            for i in range(
                0,
                len(result_text),
                max_length
            ):
                chunk = result_text[
                    i:i + max_length
                ]

                await update.message.reply_text(
                    chunk,
                    parse_mode=None,
                )

    except requests.HTTPError as e:
        logger.error(
            "HTTP ошибка Skysmart API: %s",
            e
        )

        try:
            await processing_message.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Skysmart API временно недоступен.\n\n"
            "Попробуйте отправить ссылку ещё раз через некоторое время.",
            parse_mode=None,
        )

    except Exception as e:
        logger.exception(
            "Ошибка обработки задания: %s",
            e
        )

        try:
            await processing_message.delete()
        except Exception:
            pass

        await update.message.reply_text(
            "❌ Произошла ошибка при получении ответов.\n\n"
            f"{str(e)[:1000]}",
            parse_mode=None,
        )


async def health(request):
    return JSONResponse(
        {"status": "ok"}
    )


async def root(request):
    return PlainTextResponse(
        "Bot is running."
    )


application = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


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
        text_handler
    )
)


async def telegram_webhook(request: Request):
    try:
        data = await request.json()

        update = Update.de_json(
            data,
            application.bot
        )

        await application.process_update(
            update
        )

        return JSONResponse(
            {"ok": True}
        )

    except Exception as e:
        logger.exception(
            "Ошибка webhook: %s",
            e
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e),
            },
            status_code=500,
        )


async def startup():
    initialize_passwords()

    await application.initialize()

    await application.start()

    webhook_url = (
        RENDER_URL.rstrip("/")
        + WEBHOOK_PATH
    )

    logger.info(
        "Устанавливаем webhook: %s",
        webhook_url
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    logger.info(
        "Webhook установлен."
    )


async def shutdown():
    await application.stop()
    await application.shutdown()


app = Starlette(
    routes=[
        Route(
            "/",
            root,
            methods=["GET"]
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


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
    )
