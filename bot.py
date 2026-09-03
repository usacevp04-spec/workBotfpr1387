import os
import re
import html
import hashlib
import secrets
import asyncio

import requests
import uvicorn

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from supabase import create_client


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

OWNER_ID = os.getenv("OWNER_ID")

PORT = int(os.getenv("PORT", "10000"))

SKYSMART_API = "https://skysmart-answers.vercel.app/get_answers/"

MAX_MESSAGE_LENGTH = 3900


# ============================================================
# ПРОВЕРКА НАСТРОЕК
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not RENDER_URL:
    raise RuntimeError("Не задан RENDER_URL")

if not SUPABASE_URL:
    raise RuntimeError("Не задан SUPABASE_URL")

if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("Не задан SUPABASE_SERVICE_KEY")

if not OWNER_ID:
    raise RuntimeError("Не задан OWNER_ID")


OWNER_ID = int(OWNER_ID)


# ============================================================
# SUPABASE
# ============================================================

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


# ============================================================
# ПАРОЛИ
# ============================================================

def hash_password(password: str) -> str:
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def generate_password() -> str:
    """
    Формат:

    XXXX-XXXX
    """

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    first = "".join(
        secrets.choice(alphabet)
        for _ in range(4)
    )

    second = "".join(
        secrets.choice(alphabet)
        for _ in range(4)
    )

    return f"{first}-{second}"


def initialize_passwords():
    """
    Создаёт пароли до тех пор,
    пока свободных не станет 10.
    """

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

            # Защита от случайного совпадения
            existing = (
                supabase
                .table("bot_passwords")
                .select("id")
                .eq("password_hash", password_hash)
                .execute()
            )

            if existing.data:
                continue

            supabase.table("bot_passwords").insert({
                "password_hash": password_hash,
                "password_text": password,
                "used": False,
                "used_by": None,
                "used_at": None,
            }).execute()

            current_count += 1

        print(f"Пароли: свободных {current_count}")

    except Exception as e:
        print(f"Ошибка создания паролей: {e}")


# ============================================================
# ПОЛЬЗОВАТЕЛИ
# ============================================================

def is_authorized(user_id: int) -> bool:

    try:
        result = (
            supabase
            .table("bot_users")
            .select("authorized")
            .eq("telegram_id", user_id)
            .limit(1)
            .execute()
        )

        if not result.data:
            return False

        return bool(result.data[0].get("authorized"))

    except Exception as e:

        print(f"Ошибка проверки пользователя: {e}")

        return False


def create_user_if_needed(user_id: int):

    try:

        result = (
            supabase
            .table("bot_users")
            .select("telegram_id")
            .eq("telegram_id", user_id)
            .limit(1)
            .execute()
        )

        if not result.data:

            supabase.table("bot_users").insert({
                "telegram_id": user_id,
                "authorized": False,
            }).execute()

    except Exception as e:

        print(f"Ошибка создания пользователя: {e}")


def use_password(
    password: str,
    user_id: int
) -> bool:

    password = password.strip().upper()

    password_hash = hash_password(password)

    try:

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

        # Используем пароль
        supabase.table("bot_passwords").update({
            "used": True,
            "used_by": user_id,
        }).eq(
            "id",
            password_row["id"]
        ).execute()

        # Авторизуем пользователя
        supabase.table("bot_users").update({
            "authorized": True,
        }).eq(
            "telegram_id",
            user_id
        ).execute()

        return True

    except Exception as e:

        print(f"Ошибка использования пароля: {e}")

        return False


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    user_id = update.effective_user.id

    create_user_if_needed(user_id)

    if is_authorized(user_id):

        await update.message.reply_text(
            "✅ Вы уже авторизованы.\n\n"
            "Отправьте ссылку на тест Skysmart."
        )

        return

    await update.message.reply_text(
        "🔐 Для использования бота нужен пароль.\n\n"
        "Введите пароль:"
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
        f"`{update.effective_user.id}`",
        parse_mode="Markdown"
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

    user_id = update.effective_user.id

    if user_id != OWNER_ID:

        await update.message.reply_text(
            "⛔ У вас нет доступа."
        )

        return

    try:

        result = (
            supabase
            .table("bot_passwords")
            .select("*")
            .eq("used", False)
            .order("id")
            .execute()
        )

        passwords = result.data or []

        if not passwords:

            await update.message.reply_text(
                "Свободных паролей нет."
            )

            return

        text = "🔑 Свободные пароли:\n\n"

        for index, row in enumerate(passwords, 1):

            text += (
                f"{index}. `{row['password_text']}`\n"
            )

        await update.message.reply_text(
            text,
            parse_mode="Markdown"
        )

    except Exception as e:

        print(f"Ошибка /keys: {e}")

        await update.message.reply_text(
            "❌ Не удалось получить список паролей."
        )


# ============================================================
# LATEX → ЧИТАЕМЫЙ ВИД
# ============================================================

def latex_to_readable(text: str) -> str:

    if not text:
        return text

    # HTML entities
    text = (
        text
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&ge;", "≥")
        .replace("&le;", "≤")
        .replace("&amp;", "&")
    )

    # Backslash-варианты
    text = re.sub(
        r"\\\s*gt\b",
        ">",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\\\s*lt\b",
        "<",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\\\s*ge\b",
        "≥",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\\\s*le\b",
        "≤",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\\\s*neq\b",
        "≠",
        text,
        flags=re.IGNORECASE
    )

    # Plain-варианты
    text = re.sub(
        r"\bgt\b",
        ">",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\blt\b",
        "<",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\bge\b",
        "≥",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\ble\b",
        "≤",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\bneq\b",
        "≠",
        text,
        flags=re.IGNORECASE
    )

    # Дроби
    def replace_frac(match):

        numerator = match.group(1)
        denominator = match.group(2)

        return f"({numerator}/{denominator})"

    text = re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*"
        r"\{([^{}]*)\}\s*"
        r"\{([^{}]*)\}",
        replace_frac,
        text
    )

    # Корень
    def replace_sqrt(match):

        content = match.group(1)

        return f"√({content})"

    text = re.sub(
        r"\\sqrt\s*\{([^{}]*)\}",
        replace_sqrt,
        text
    )

    # Степени
    superscript_map = str.maketrans(
        "0123456789+-=()nix",
        "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ"
    )

    def replace_power(match):

        content = match.group(1)

        if all(
            char in "0123456789+-=()nix"
            for char in content
        ):
            return content.translate(
                superscript_map
            )

        return f"^({content})"

    text = re.sub(
        r"\^\s*\{([^{}]*)\}",
        replace_power,
        text
    )

    # Символы
    replacements = {
        r"\mathbb{R}": "ℝ",
        r"\mathbb R": "ℝ",
        r"\R": "ℝ",
        r"\infty": "∞",

        r"\leq": "≤",
        r"\le": "≤",
        r"\geq": "≥",
        r"\ge": "≥",

        r"\neq": "≠",
        r"\ne": "≠",

        r"\pm": "±",
        r"\mp": "∓",

        r"\times": "×",
        r"\cdot": "·",
        r"\div": "÷",

        r"\in": "∈",
        r"\notin": "∉",

        r"\subset": "⊂",
        r"\subseteq": "⊆",

        r"\cup": "∪",
        r"\cap": "∩",

        r"\rightarrow": "→",
        r"\to": "→",
        r"\leftarrow": "←",

        r"\Rightarrow": "⇒",
        r"\Leftrightarrow": "⇔",

        r"\approx": "≈",
        r"\sim": "∼",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Убираем визуальные LaTeX-команды
    text = re.sub(
        r"\\(?:Bigg|bigg|Big|big|left|right|middle)\b",
        "",
        text
    )

    # Убираем spacing-команды
    text = re.sub(
        r"\\[,;:!]\s*",
        "",
        text
    )

    # \text{...}
    text = re.sub(
        r"\\text\s*\{([^{}]*)\}",
        r"\1",
        text
    )

    # \mathrm{...}
    text = re.sub(
        r"\\mathrm\s*\{([^{}]*)\}",
        r"\1",
        text
    )

    # \operatorname{...}
    text = re.sub(
        r"\\operatorname\s*\{([^{}]*)\}",
        r"\1",
        text
    )

    # Оставшиеся команды
    text = re.sub(
        r"\\[a-zA-Z]+\b",
        "",
        text
    )

    # Убираем лишние фигурные скобки
    text = text.replace("{", "")
    text = text.replace("}", "")

    # Убираем обратный слеш перед настоящими символами
    text = re.sub(
        r"\\\s*(>)",
        r"\1",
        text
    )

    text = re.sub(
        r"\\\s*(<)",
        r"\1",
        text
    )

    text = re.sub(
        r"\\\s*(≥)",
        r"\1",
        text
    )

    text = re.sub(
        r"\\\s*(≤)",
        r"\1",
        text
    )

    text = re.sub(
        r"\\\s*(≠)",
        r"\1",
        text
    )

    # Пробелы
    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


def clean_skysmart_text(value) -> str:

    if value is None:
        return ""

    text = str(value)

    text = html.unescape(text)

    text = latex_to_readable(text)

    return text.strip()


# ============================================================
# SKYSMART URL
# ============================================================

def extract_room_name(text: str):

    pattern = (
        r"https?://edu\.skysmart\.ru/"
        r"student/([^?\s]+)"
    )

    match = re.search(
        pattern,
        text,
        flags=re.IGNORECASE
    )

    if not match:
        return None

    room = match.group(1)

    # На случай ссылки с лишними символами
    room = room.rstrip(".,!?)]}>")

    return room


# ============================================================
# SKYSMART API
# ============================================================

def get_skysmart_answers(room_name: str):

    response = requests.post(
        SKYSMART_API,
        json={
            "roomName": room_name
        },
        timeout=30
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# ФОРМАТИРОВАНИЕ SKYSMART
# ============================================================

def get_task_number(task, index):

    if not isinstance(task, dict):
        return index

    for key in (
        "number",
        "task_number",
        "taskNumber",
        "id",
    ):

        if key in task:

            value = task[key]

            if value is not None:
                return value

    return index


def get_task_question(task):

    if not isinstance(task, dict):
        return ""

    possible_keys = [
        "question",
        "question_text",
        "questionText",
        "text",
        "title",
        "condition",
        "task",
    ]

    for key in possible_keys:

        value = task.get(key)

        if value not in (None, ""):

            return clean_skysmart_text(value)

    return ""


def get_task_answer(task):

    if not isinstance(task, dict):
        return ""

    possible_keys = [
        "answer",
        "answers",
        "correct_answer",
        "correctAnswer",
        "result",
        "solution",
    ]

    for key in possible_keys:

        value = task.get(key)

        if value not in (None, ""):

            if isinstance(value, list):

                return ", ".join(
                    clean_skysmart_text(x)
                    for x in value
                )

            if isinstance(value, dict):

                return clean_skysmart_text(
                    value.get("text")
                    or value.get("value")
                    or value
                )

            return clean_skysmart_text(value)

    return ""


def format_skysmart(data):

    """
    Основной формат результата.
    """

    if not isinstance(data, list):
        return "❌ Неожиданный ответ от сервера."

    if not data:
        return "❌ Ответ пустой."

    tasks = data[0]

    if not isinstance(tasks, list):
        return "❌ Не удалось получить список заданий."

    lines = []

    for index, task in enumerate(tasks, 1):

        number = get_task_number(
            task,
            index
        )

        question = get_task_question(task)
        answer = get_task_answer(task)

        if not question:
            question = "—"

        if not answer:
            answer = "—"

        lines.append(
            f"<b>Задание {html.escape(str(number))}</b>\n"
            f"❓ {html.escape(question)}\n"
            f"✅ <b>Ответ:</b> {html.escape(answer)}"
        )

    return "\n\n".join(lines)


# ============================================================
# РАЗБИВКА ДЛИННОГО СООБЩЕНИЯ
# ============================================================

def split_message(
    text: str,
    max_length: int = MAX_MESSAGE_LENGTH
):

    if len(text) <= max_length:
        return [text]

    chunks = []

    current = ""

    blocks = text.split("\n\n")

    for block in blocks:

        if len(block) > max_length:

            if current:
                chunks.append(current)
                current = ""

            for i in range(
                0,
                len(block),
                max_length
            ):
                chunks.append(
                    block[i:i + max_length]
                )

            continue

        candidate = (
            block
            if not current
            else current + "\n\n" + block
        )

        if len(candidate) <= max_length:

            current = candidate

        else:

            if current:
                chunks.append(current)

            current = block

    if current:
        chunks.append(current)

    return chunks


async def send_long_message(
    message,
    text: str
):

    chunks = split_message(text)

    for chunk in chunks:

        await message.reply_text(
            chunk,
            parse_mode="HTML",
            disable_web_page_preview=True
        )


# ============================================================
# ОБРАБОТКА СООБЩЕНИЙ
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    user_id = update.effective_user.id

    create_user_if_needed(user_id)

    text = update.message.text or ""

    text = text.strip()

    if not text:
        return

    # --------------------------------------------------------
    # Если пользователь ещё не авторизован
    # --------------------------------------------------------

    if not is_authorized(user_id):

        if use_password(text, user_id):

            await update.message.reply_text(
                "✅ Пароль принят!\n\n"
                "Авторизация успешна.\n\n"
                "Теперь отправьте ссылку на тест Skysmart."
            )

        else:

            await update.message.reply_text(
                "❌ Неверный или уже использованный пароль."
            )

        return

    # --------------------------------------------------------
    # Проверяем ссылку Skysmart
    # --------------------------------------------------------

    room_name = extract_room_name(text)

    if not room_name:

        await update.message.reply_text(
            "❗ Отправьте ссылку на тест Skysmart.\n\n"
            "Пример:\n"
            "https://edu.skysmart.ru/student/..."
        )

        return

    # --------------------------------------------------------
    # Получаем ответы
    # --------------------------------------------------------

    processing_message = await update.message.reply_text(
        "⏳ Получаю задания и ответы..."
    )

    try:

        # requests блокирует event loop,
        # поэтому выполняем запрос в отдельном потоке
        data = await asyncio.to_thread(
            get_skysmart_answers,
            room_name
        )

        result = format_skysmart(data)

        await processing_message.delete()

        await send_long_message(
            update.message,
            result
        )

    except requests.Timeout:

        await processing_message.edit_text(
            "❌ Сервер Skysmart слишком долго отвечает.\n"
            "Попробуйте ещё раз."
        )

    except requests.RequestException as e:

        print(f"Skysmart HTTP error: {e}")

        await processing_message.edit_text(
            "❌ Не удалось получить ответы от Skysmart.\n"
            "Попробуйте ещё раз."
        )

    except Exception as e:

        print(f"Skysmart error: {e}")

        await processing_message.edit_text(
            "❌ Произошла ошибка при обработке теста."
        )


# ============================================================
# TELEGRAM HANDLERS
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

WEBHOOK_PATH = "/telegram"


async def telegram_webhook(
    request: Request
):

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            application.bot
        )

        await application.process_update(
            update
        )

        return PlainTextResponse(
            "OK"
        )

    except Exception as e:

        print(
            f"Ошибка Telegram webhook: {e}"
        )

        return PlainTextResponse(
            "ERROR",
            status_code=500
        )


# ============================================================
# STARLETTE
# ============================================================

async def homepage(
    request: Request
):

    return PlainTextResponse(
        "Telegram bot is running."
    )


async def health(
    request: Request
):

    return PlainTextResponse(
        "OK"
    )


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def startup():

    print()
    print("========================================")
    print("Запуск Telegram бота...")
    print("========================================")

    try:

        initialize_passwords()

        await application.initialize()

        await application.start()

        webhook_url = (
            RENDER_URL +
            WEBHOOK_PATH
        )

        await application.bot.set_webhook(
            url=webhook_url
        )

        print(
            f"Webhook установлен:\n"
            f"{webhook_url}"
        )

        print("Бот успешно запущен!")

    except Exception as e:

        print(
            f"ОШИБКА ЗАПУСКА БОТА: {e}"
        )

        raise


async def shutdown():

    print(
        "Остановка Telegram бота..."
    )

    try:

        await application.stop()

        await application.shutdown()

    except Exception as e:

        print(
            f"Ошибка остановки: {e}"
        )


app = Starlette(
    routes=[],
    on_startup=[startup],
    on_shutdown=[shutdown]
)


# ============================================================
# ROUTES
# ============================================================

from starlette.routing import Route

app.router.routes.extend([
    Route(
        "/",
        homepage,
        methods=["GET", "HEAD"]
    ),

    Route(
        "/health",
        health,
        methods=["GET"]
    ),

    Route(
        "/telegram",
        telegram_webhook,
        methods=["POST"]
    ),
])


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )
