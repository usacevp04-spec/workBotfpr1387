import os
import asyncio
import re
import html
import secrets
import hashlib

import requests
import uvicorn

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from supabase import create_client


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))

PORT = int(os.getenv("PORT", "10000"))

MAX_MESSAGE_LENGTH = 3900
MAX_INPUT_MESSAGES = 50
MAX_INPUT_LENGTH = 100000

SKYSMART_API = "https://skysmart-answers.vercel.app/get_answers/"


# ============================================================
# SUPABASE
# ============================================================

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)


# ============================================================
# MEMORY
# ============================================================

# Обычные входящие сообщения пользователя
user_buffers = {}

# ID последнего сообщения бота с кнопкой "Завершить ввод"
user_last_control_message = {}

# Последние обычные задания
user_last_texts = {}

# Последние данные Skysmart
user_last_skysmart_data = {}


# ============================================================
# BASIC HELPERS
# ============================================================

def hash_password(password: str) -> str:
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def generate_password() -> str:
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


# ============================================================
# SUPABASE AUTH
# ============================================================

def create_user_if_needed(user_id: int):

    try:
        result = (
            supabase
            .table("bot_users")
            .select("*")
            .eq("telegram_id", user_id)
            .execute()
        )

        if not result.data:
            supabase.table("bot_users").insert({
                "telegram_id": user_id,
                "authorized": False
            }).execute()

    except Exception as e:
        print("create_user_if_needed error:", e)


def is_authorized(user_id: int) -> bool:

    try:
        result = (
            supabase
            .table("bot_users")
            .select("authorized")
            .eq("telegram_id", user_id)
            .execute()
        )

        if not result.data:
            return False

        return bool(
            result.data[0].get("authorized", False)
        )

    except Exception as e:
        print("is_authorized error:", e)
        return False


def use_password(
    user_id: int,
    password: str
) -> bool:

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

        password_id = result.data[0]["id"]

        (
            supabase
            .table("bot_passwords")
            .update({
                "used": True,
                "used_by": user_id,
                "used_at": "now()"
            })
            .eq("id", password_id)
            .execute()
        )

        (
            supabase
            .table("bot_users")
            .update({
                "authorized": True
            })
            .eq("telegram_id", user_id)
            .execute()
        )

        return True

    except Exception as e:
        print("use_password error:", e)
        return False


def initialize_passwords():

    try:

        result = (
            supabase
            .table("bot_passwords")
            .select("id")
            .eq("used", False)
            .execute()
        )

        existing = len(result.data or [])

        needed = max(
            0,
            10 - existing
        )

        for _ in range(needed):

            password = generate_password()

            supabase.table("bot_passwords").insert({
                "password_hash": hash_password(password),
                "password_text": password,
                "used": False
            }).execute()

        print(
            f"Passwords initialized. Added: {needed}"
        )

    except Exception as e:
        print(
            "initialize_passwords error:",
            e
        )


# ============================================================
# KEYBOARDS
# ============================================================

def finish_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏹ Завершить ввод",
                callback_data="finish_input"
            )
        ]
    ])


def result_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📱 Удобно",
                callback_data="format_mobile"
            ),
            InlineKeyboardButton(
                "📝 Список",
                callback_data="format_list"
            )
        ],
        [
            InlineKeyboardButton(
                "⚡ Только ответы",
                callback_data="format_compact"
            ),
            InlineKeyboardButton(
                "🔄 Заново",
                callback_data="format_repeat"
            )
        ]
    ])


# ============================================================
# TELEGRAM MESSAGE SPLITTER
# ============================================================

def split_by_telegram_messages(
    text: str,
    limit: int = MAX_MESSAGE_LENGTH
):

    if not text:
        return [""]

    chunks = []

    while len(text) > limit:

        cut = text.rfind(
            "\n",
            0,
            limit
        )

        if cut <= 0:
            cut = limit

        chunks.append(
            text[:cut]
        )

        text = text[cut:].lstrip("\n")

    if text:
        chunks.append(text)

    return chunks


async def send_long_message(
    message,
    text: str,
    reply_markup=None,
    parse_mode="HTML"
):

    chunks = split_by_telegram_messages(
        text
    )

    for i, chunk in enumerate(chunks):

        await message.reply_text(
            chunk,
            parse_mode=parse_mode,
            reply_markup=(
                reply_markup
                if i == len(chunks) - 1
                else None
            ),
            disable_web_page_preview=True
        )


# ============================================================
# LATEX -> READABLE
# ============================================================

def latex_to_readable(text: str) -> str:

    if not text:
        return ""

    # --------------------------------------------------------
    # HTML entities
    # --------------------------------------------------------

    text = (
        text
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&ge;", "≥")
        .replace("&le;", "≤")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )

    # --------------------------------------------------------
    # Backslash comparison commands
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Plain gt / lt / ge / le / neq
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Fractions
    # --------------------------------------------------------

    def replace_fraction(match):

        numerator = match.group(1)
        denominator = match.group(2)

        return (
            "("
            + numerator.strip()
            + "/"
            + denominator.strip()
            + ")"
        )

    # Несколько проходов для вложенных конструкций
    for _ in range(5):

        new_text = re.sub(
            r"\\(?:dfrac|tfrac|frac)\{([^{}]*)\}\{([^{}]*)\}",
            replace_fraction,
            text
        )

        if new_text == text:
            break

        text = new_text

    # --------------------------------------------------------
    # Square roots
    # --------------------------------------------------------

    text = re.sub(
        r"\\sqrt\{([^{}]*)\}",
        r"√(\1)",
        text
    )

    text = re.sub(
        r"\\sqrt\s*([A-Za-z0-9]+)",
        r"√(\1)",
        text
    )

    # --------------------------------------------------------
    # Unicode superscripts
    # --------------------------------------------------------

    superscripts = {
        "0": "⁰",
        "1": "¹",
        "2": "²",
        "3": "³",
        "4": "⁴",
        "5": "⁵",
        "6": "⁶",
        "7": "⁷",
        "8": "⁸",
        "9": "⁹",
        "+": "⁺",
        "-": "⁻",
        "=": "⁼",
        "(": "⁽",
        ")": "⁾",
        "n": "ⁿ",
        "i": "ⁱ",
    }

    def convert_superscript(match):

        content = match.group(1)

        result = ""

        for char in content:
            result += superscripts.get(
                char,
                char
            )

        return result

    text = re.sub(
        r"\^\{([^{}]+)\}",
        convert_superscript,
        text
    )

    text = re.sub(
        r"\^([A-Za-z0-9])",
        lambda m: superscripts.get(
            m.group(1),
            m.group(1)
        ),
        text
    )

    # --------------------------------------------------------
    # Subscripts
    # --------------------------------------------------------

    subscripts = {
        "0": "₀",
        "1": "₁",
        "2": "₂",
        "3": "₃",
        "4": "₄",
        "5": "₅",
        "6": "₆",
        "7": "₇",
        "8": "₈",
        "9": "₉",
        "+": "₊",
        "-": "₋",
        "=": "₌",
        "(": "₍",
        ")": "₎",
        "a": "ₐ",
        "e": "ₑ",
        "h": "ₕ",
        "i": "ᵢ",
        "j": "ⱼ",
        "k": "ₖ",
        "l": "ₗ",
        "m": "ₘ",
        "n": "ₙ",
        "o": "ₒ",
        "p": "ₚ",
        "r": "ᵣ",
        "s": "ₛ",
        "t": "ₜ",
        "u": "ᵤ",
        "v": "ᵥ",
        "x": "ₓ",
    }

    def convert_subscript(match):

        content = match.group(1)

        result = ""

        for char in content:
            result += subscripts.get(
                char,
                char
            )

        return result

    text = re.sub(
        r"_\{([^{}]+)\}",
        convert_subscript,
        text
    )

    text = re.sub(
        r"_([A-Za-z0-9])",
        lambda m: subscripts.get(
            m.group(1),
            m.group(1)
        ),
        text
    )

    # --------------------------------------------------------
    # Common LaTeX symbols
    # --------------------------------------------------------

    replacements = {

        r"\mathbb{R}": "ℝ",
        r"\mathbb{N}": "ℕ",
        r"\mathbb{Z}": "ℤ",
        r"\mathbb{Q}": "ℚ",

        r"\R": "ℝ",
        r"\N": "ℕ",
        r"\Z": "ℤ",
        r"\Q": "ℚ",

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

        r"\supset": "⊃",
        r"\supseteq": "⊇",

        r"\cup": "∪",
        r"\cap": "∩",

        r"\emptyset": "∅",

        r"\rightarrow": "→",
        r"\to": "→",

        r"\leftarrow": "←",

        r"\Rightarrow": "⇒",
        r"\Leftarrow": "⇐",

        r"\leftrightarrow": "↔",

        r"\approx": "≈",
        r"\sim": "∼",

        r"\angle": "∠",
        r"\triangle": "△",

        r"\degree": "°",

        r"\alpha": "α",
        r"\beta": "β",
        r"\gamma": "γ",
        r"\delta": "δ",
        r"\epsilon": "ε",
        r"\theta": "θ",
        r"\lambda": "λ",
        r"\mu": "μ",
        r"\pi": "π",
        r"\sigma": "σ",
        r"\phi": "φ",
        r"\omega": "ω",
    }

    for old, new in replacements.items():
        text = text.replace(
            old,
            new
        )

    # --------------------------------------------------------
    # \text{...}
    # --------------------------------------------------------

    text = re.sub(
        r"\\text\{([^{}]*)\}",
        r"\1",
        text
    )

    text = re.sub(
        r"\\mathrm\{([^{}]*)\}",
        r"\1",
        text
    )

    text = re.sub(
        r"\\operatorname\{([^{}]*)\}",
        r"\1",
        text
    )

    # --------------------------------------------------------
    # Big / left / right
    # --------------------------------------------------------

    text = re.sub(
        r"\\(?:Bigg|bigg|Big|big|left|right|middle)\b",
        "",
        text
    )

    # --------------------------------------------------------
    # Spacing commands
    # --------------------------------------------------------

    text = re.sub(
        r"\\[,;:!]\s*",
        " ",
        text
    )

    text = re.sub(
        r"\\quad\b",
        " ",
        text
    )

    text = re.sub(
        r"\\qquad\b",
        " ",
        text
    )

    # --------------------------------------------------------
    # Remaining LaTeX commands
    # --------------------------------------------------------

    text = re.sub(
        r"\\[A-Za-z]+\b",
        "",
        text
    )

    # --------------------------------------------------------
    # Remove LaTeX braces
    # --------------------------------------------------------

    text = text.replace(
        "{",
        ""
    ).replace(
        "}",
        ""
    )

    # --------------------------------------------------------
    # Remove stray slash before actual symbols
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Spaces
    # --------------------------------------------------------

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


def clean_skysmart_text(text) -> str:

    if text is None:
        return ""

    text = str(text)

    text = html.unescape(text)

    text = latex_to_readable(text)

    return text.strip()


# ============================================================
# ORDINARY TASK PARSER
# ============================================================

def remove_choice_options(question: str) -> str:

    lines = question.splitlines()

    result = []

    option_pattern = re.compile(
        r"^\s*(?:"
        r"[A-HА-Я]\)"
        r"|[A-HА-Я]\."
        r"|[A-HА-Я]\s*[-–—]"
        r"|\d+\)"
        r"|\d+\."
        r")\s+"
    )

    for line in lines:

        if option_pattern.match(line):
            continue

        result.append(line)

    question = "\n".join(result)

    return question.strip()


def clean_question_text(question: str) -> str:

    if not question:
        return ""

    question = re.sub(
        r"Получил сообщение.*",
        "",
        question,
        flags=re.IGNORECASE
    )

    question = re.sub(
        r"\n\s*Получил сообщение.*",
        "",
        question,
        flags=re.IGNORECASE
    )

    question = remove_choice_options(
        question
    )

    return question.strip()


def parse_questions(text: str):

    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    # Удаляем служебный текст
    text = re.sub(
        r"Получил сообщение.*?(?=\n|$)",
        "",
        text,
        flags=re.IGNORECASE
    )

    pattern = re.compile(
        r"(?:"
        r"Вопрос"
        r"|Question"
        r")\s*"
        r"(\d+)"
        r"\s*:\s*"
        r"(.*?)"
        r"(?:"
        r"\n\s*Ответ\s*:"
        r"|\n\s*Answer\s*:"
        r")"
        r"\s*"
        r"(.*?)"
        r"(?="
        r"\n\s*(?:Вопрос|Question)\s*\d+\s*:"
        r"|$"
        r")",
        re.IGNORECASE | re.DOTALL
    )

    questions = []

    for match in pattern.finditer(text):

        number = match.group(1).strip()

        question = match.group(2).strip()

        answer = match.group(3).strip()

        question = clean_question_text(
            question
        )

        answer = answer.strip()

        if not question and not answer:
            continue

        questions.append({
            "number": number,
            "question": question,
            "answer": answer
        })

    return questions


# ============================================================
# ORDINARY FORMATTERS
# ============================================================

def make_mobile(questions):

    parts = []

    for item in questions:

        number = html.escape(
            str(item["number"])
        )

        question = html.escape(
            item["question"]
        )

        answer = html.escape(
            item["answer"]
        )

        parts.append(
            f"<b>{number}. {question}</b>\n"
            f"✅ {answer}"
        )

    return "\n\n".join(parts)


def make_list(questions):

    parts = []

    for item in questions:

        number = html.escape(
            str(item["number"])
        )

        answer = html.escape(
            item["answer"]
        )

        parts.append(
            f"{number}. {answer}"
        )

    return "\n".join(parts)


def make_compact(questions):

    answers = []

    for item in questions:

        answer = item["answer"].strip()

        if answer:
            answers.append(answer)

    # Для compact используем plain text,
    # поэтому < и > не ломаются.
    return ", ".join(answers)


# ============================================================
# SKYSMART PARSER
# ============================================================

def prepare_skysmart_tasks(data):

    tasks = []

    if not isinstance(data, list):
        return tasks

    if len(data) == 0:
        return tasks

    raw_tasks = data[0]

    if not isinstance(raw_tasks, list):
        return tasks

    for index, task in enumerate(
        raw_tasks,
        start=1
    ):

        if not isinstance(task, dict):
            continue

        number = (
            task.get("number")
            or task.get("task_number")
            or task.get("id")
            or index
        )

        question = (
            task.get("question")
            or task.get("text")
            or task.get("task")
            or ""
        )

        answer = (
            task.get("answer")
            or task.get("correct_answer")
            or task.get("result")
            or ""
        )

        question = clean_skysmart_text(
            question
        )

        answer = clean_skysmart_text(
            answer
        )

        if not question and not answer:
            continue

        tasks.append({
            "number": str(number),
            "question": question,
            "answer": answer
        })

    return tasks


def make_skysmart_mobile(data):

    tasks = prepare_skysmart_tasks(
        data
    )

    parts = []

    for task in tasks:

        number = html.escape(
            task["number"]
        )

        question = html.escape(
            task["question"]
        )

        answer = html.escape(
            task["answer"]
        )

        parts.append(
            f"<b>{number}. {question}</b>\n"
            f"✅ {answer}"
        )

    return "\n\n".join(parts)


def make_skysmart_list(data):

    tasks = prepare_skysmart_tasks(
        data
    )

    parts = []

    for task in tasks:

        number = html.escape(
            task["number"]
        )

        answer = html.escape(
            task["answer"]
        )

        parts.append(
            f"{number}. {answer}"
        )

    return "\n".join(parts)


def make_skysmart_compact(data):

    tasks = prepare_skysmart_tasks(
        data
    )

    answers = []

    for task in tasks:

        answer = task["answer"].strip()

        if answer:
            answers.append(answer)

    # ВАЖНО:
    # compact отправляется без HTML,
    # поэтому знаки < и > отображаются как есть.
    return ", ".join(answers)


# ============================================================
# SKYSMART URL
# ============================================================

def extract_skysmart_room(text: str):

    if not text:
        return None

    match = re.search(
        r"https?://edu\.skysmart\.ru/student/([A-Za-z0-9_-]+)",
        text
    )

    if not match:
        return None

    return match.group(1)


async def get_skysmart_answers(
    room_name: str
):

    def request():

        response = requests.post(
            SKYSMART_API,
            json={
                "roomName": room_name
            },
            timeout=30
        )

        response.raise_for_status()

        return response.json()

    return await asyncio.to_thread(
        request
    )


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user is None:
        return

    user_id = user.id

    create_user_if_needed(
        user_id
    )

    if is_authorized(user_id):

        await update.message.reply_text(
            "✅ Вы уже авторизованы.\n\n"
            "Отправляйте задания или ссылку Skysmart."
        )

        return

    await update.message.reply_text(
        "🔐 Для использования бота нужен пароль.\n\n"
        "Отправьте пароль сообщением."
    )


# ============================================================
# /ID
# ============================================================

async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user is None:
        return

    await update.message.reply_text(
        f"🆔 Ваш Telegram ID:\n\n"
        f"<code>{user.id}</code>",
        parse_mode="HTML"
    )


# ============================================================
# /KEYS
# ============================================================

async def keys_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user is None:
        return

    if user.id != OWNER_ID:

        await update.message.reply_text(
            "⛔ Недостаточно прав."
        )

        return

    try:

        result = (
            supabase
            .table("bot_passwords")
            .select("password_text, used, used_by, used_at")
            .order("id")
            .execute()
        )

        rows = result.data or []

        if not rows:

            await update.message.reply_text(
                "Паролей пока нет."
            )

            return

        lines = [
            "🔑 <b>Пароли:</b>\n"
        ]

        for index, row in enumerate(
            rows,
            start=1
        ):

            password = html.escape(
                str(
                    row.get(
                        "password_text",
                        ""
                    )
                )
            )

            used = row.get(
                "used",
                False
            )

            status = (
                "❌ использован"
                if used
                else "✅ свободен"
            )

            lines.append(
                f"{index}. <code>{password}</code> — {status}"
            )

        await send_long_message(
            update.message,
            "\n".join(lines),
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            "/keys error:",
            e
        )

        await update.message.reply_text(
            "❌ Не удалось получить список ключей."
        )


# ============================================================
# PASSWORD HANDLER
# ============================================================

async def try_authorize_with_password(
    update: Update,
    text: str
):

    user = update.effective_user

    if user is None:
        return False

    user_id = user.id

    if is_authorized(user_id):
        return False

    password = text.strip()

    if not re.fullmatch(
        r"[A-Za-z0-9]{4}-[A-Za-z0-9]{4}",
        password
    ):
        return False

    if use_password(
        user_id,
        password
    ):

        await update.message.reply_text(
            "✅ Пароль принят!\n\n"
            "Теперь бот готов к работе."
        )

        return True

    await update.message.reply_text(
        "❌ Неверный или уже использованный пароль."
    )

    return True


# ============================================================
# ORDINARY MESSAGE
# ============================================================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message

    if message is None:
        return

    user = update.effective_user

    if user is None:
        return

    user_id = user.id

    text = message.text or ""

    if not text.strip():
        return

    create_user_if_needed(
        user_id
    )

    # --------------------------------------------------------
    # Authorization
    # --------------------------------------------------------

    if not is_authorized(user_id):

        handled = await try_authorize_with_password(
            update,
            text
        )

        if handled:
            return

        await message.reply_text(
            "🔐 Сначала отправьте пароль."
        )

        return

    # --------------------------------------------------------
    # SKYSMART
    # --------------------------------------------------------

    room_name = extract_skysmart_room(
        text
    )

    if room_name:

        await message.reply_text(
            "⏳ Получаю ответы из Skysmart..."
        )

        try:

            data = await get_skysmart_answers(
                room_name
            )

            if not data:

                await message.reply_text(
                    "❌ Skysmart не вернул данные."
                )

                return

            user_last_skysmart_data[
                user_id
            ] = data

            # Убираем старые обычные данные,
            # чтобы форматы не смешивались.
            user_last_texts.pop(
                user_id,
                None
            )

            output = make_skysmart_mobile(
                data
            )

            if not output:

                await message.reply_text(
                    "❌ Не удалось найти задания в ответе Skysmart."
                )

                return

            await send_long_message(
                message,
                output,
                reply_markup=result_keyboard(),
                parse_mode="HTML"
            )

        except Exception as e:

            print(
                "Skysmart error:",
                e
            )

            await message.reply_text(
                "❌ Ошибка при получении ответов Skysmart."
            )

        return

    # --------------------------------------------------------
    # ORDINARY INPUT
    # --------------------------------------------------------

    # Если пользователь начал обычный ввод,
    # старые данные Skysmart больше не должны использоваться.
    user_last_skysmart_data.pop(
        user_id,
        None
    )

    if user_id not in user_buffers:

        user_buffers[user_id] = []

    if (
        len(user_buffers[user_id])
        >= MAX_INPUT_MESSAGES
    ):

        await message.reply_text(
            "⚠️ Слишком много сообщений.\n"
            "Нажмите «Завершить ввод»."
        )

        return

    current_length = sum(
        len(x)
        for x in user_buffers[user_id]
    )

    if (
        current_length + len(text)
        > MAX_INPUT_LENGTH
    ):

        await message.reply_text(
            "⚠️ Слишком большой объём текста."
        )

        return

    user_buffers[user_id].append(
        text
    )

    # --------------------------------------------------------
    # Delete previous finish button
    # --------------------------------------------------------

    old_message_id = user_last_control_message.get(
        user_id
    )

    if old_message_id:

        try:

            await context.bot.edit_message_reply_markup(
                chat_id=user_id,
                message_id=old_message_id,
                reply_markup=None
            )

        except Exception:
            pass

    # --------------------------------------------------------
    # Send new finish button
    # --------------------------------------------------------

    control_message = await message.reply_text(
        "Нажмите, когда закончите ввод:",
        reply_markup=finish_keyboard()
    )

    user_last_control_message[
        user_id
    ] = control_message.message_id


# ============================================================
# FINISH INPUT
# ============================================================

async def finish_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if query is None:
        return

    await query.answer()

    user = query.from_user

    if user is None:
        return

    user_id = user.id

    messages = user_buffers.get(
        user_id,
        []
    )

    if not messages:

        await query.message.reply_text(
            "⚠️ Нет введённых данных."
        )

        return

    full_text = "\n".join(
        messages
    )

    questions = parse_questions(
        full_text
    )

    if not questions:

        await query.message.reply_text(
            "❌ Не удалось найти задания.\n\n"
            "Проверьте, что сообщения имеют формат:\n"
            "Вопрос 1: ...\n"
            "Ответ: ..."
        )

        return

    # Сохраняем обычные задания
    user_last_texts[
        user_id
    ] = questions

    # Очень важно:
    # старые Skysmart-данные удаляем.
    user_last_skysmart_data.pop(
        user_id,
        None
    )

    # Очищаем входной буфер
    user_buffers.pop(
        user_id,
        None
    )

    user_last_control_message.pop(
        user_id,
        None
    )

    output = make_mobile(
        questions
    )

    # --------------------------------------------------------
    # Убираем старую кнопку
    # --------------------------------------------------------

    try:

        await query.edit_message_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    # --------------------------------------------------------
    # Первый результат
    # --------------------------------------------------------

    await send_long_message(
        query.message,
        output,
        reply_markup=result_keyboard(),
        parse_mode="HTML"
    )


# ============================================================
# RESULT CALLBACK
# ============================================================

async def result_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if query is None:
        return

    await query.answer()

    user = query.from_user

    if user is None:
        return

    user_id = user.id

    callback = query.data

    # --------------------------------------------------------
    # Удаляем старые кнопки СРАЗУ
    # --------------------------------------------------------

    try:

        await query.edit_message_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    # ========================================================
    # SKYSMART
    # ========================================================

    if user_id in user_last_skysmart_data:

        data = user_last_skysmart_data[
            user_id
        ]

        if callback == "format_mobile":

            output = make_skysmart_mobile(
                data
            )

            parse_mode = "HTML"

        elif callback == "format_list":

            output = make_skysmart_list(
                data
            )

            parse_mode = "HTML"

        elif callback == "format_compact":

            output = make_skysmart_compact(
                data
            )

            # ВАЖНО:
            # compact отправляем БЕЗ HTML,
            # чтобы < и > не интерпретировались
            # как HTML-теги.
            parse_mode = None

        elif callback == "format_repeat":

            output = make_skysmart_mobile(
                data
            )

            parse_mode = "HTML"

        else:
            return

        if not output:

            await query.message.reply_text(
                "⚠️ Формат получился пустым."
            )

            return

        # ----------------------------------------------------
        # Новый результат
        # ----------------------------------------------------

        await send_long_message(
            query.message,
            output,
            parse_mode=parse_mode
        )

        # ----------------------------------------------------
        # НОВЫЕ КНОПКИ ВНИЗУ ЧАТА
        # ----------------------------------------------------

        await query.message.reply_text(
            "Выберите формат:",
            reply_markup=result_keyboard()
        )

        return

    # ========================================================
    # ORDINARY TASKS
    # ========================================================

    questions = user_last_texts.get(
        user_id,
        []
    )

    if not questions:

        await query.message.reply_text(
            "⚠️ Данные для форматирования не найдены."
        )

        return

    if callback == "format_mobile":

        output = make_mobile(
            questions
        )

        parse_mode = "HTML"

    elif callback == "format_list":

        output = make_list(
            questions
        )

        parse_mode = "HTML"

    elif callback == "format_compact":

        output = make_compact(
            questions
        )

        # Plain text
        parse_mode = None

    elif callback == "format_repeat":

        output = make_mobile(
            questions
        )

        parse_mode = "HTML"

    else:
        return

    if not output:

        await query.message.reply_text(
            "⚠️ Формат получился пустым."
        )

        return

    # --------------------------------------------------------
    # Новый результат
    # --------------------------------------------------------

    await send_long_message(
        query.message,
        output,
        parse_mode=parse_mode
    )

    # --------------------------------------------------------
    # НОВЫЕ КНОПКИ ПРЯМО ПОД РЕЗУЛЬТАТОМ
    # --------------------------------------------------------

    await query.message.reply_text(
        "Выберите формат:",
        reply_markup=result_keyboard()
    )


# ============================================================
# HEALTH / HOME
# ============================================================

async def home(
    request: Request
):

    return PlainTextResponse(
        "Bot is running"
    )


async def health(
    request: Request
):

    return PlainTextResponse(
        "OK"
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

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
    CallbackQueryHandler(
        finish_input,
        pattern=r"^finish_input$"
    )
)

application.add_handler(
    CallbackQueryHandler(
        result_callback,
        pattern=r"^format_(mobile|list|compact|repeat)$"
    )
)

application.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_message
    )
)


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
            "Webhook error:",
            e
        )

        return PlainTextResponse(
            "ERROR",
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
            methods=["GET"]
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
        )
    ]
)


# ============================================================
# STARTUP
# ============================================================

async def startup():

    print(
        "Starting Telegram application..."
    )

    await application.initialize()

    await application.start()

    initialize_passwords()

    if not RENDER_URL:

        print(
            "WARNING: RENDER_URL is not set."
        )

        return

    webhook_url = (
        f"{RENDER_URL.rstrip('/')}"
        f"/telegram"
    )

    try:

        await application.bot.set_webhook(
            url=webhook_url
        )

        print(
            "Webhook set:",
            webhook_url
        )

    except Exception as e:

        print(
            "Webhook error:",
            e
        )


async def shutdown():

    print(
        "Shutting down..."
    )

    try:
        await application.bot.delete_webhook()
    except Exception:
        pass

    try:
        await application.stop()
    except Exception:
        pass

    try:
        await application.shutdown()
    except Exception:
        pass


# ============================================================
# STARLETTE
# ============================================================

app = Starlette(
    routes=[
        Route(
            "/",
            home,
            methods=["GET"]
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
        )
    ],
    on_startup=[startup],
    on_shutdown=[shutdown]
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


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )
