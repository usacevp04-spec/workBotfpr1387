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
from starlette.responses import PlainTextResponse, Response
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

from supabase import create_client, Client


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

OWNER_ID = os.getenv("OWNER_ID", "")

PORT = int(os.getenv("PORT", "10000"))

MAX_MESSAGE_LENGTH = 3900
MAX_INPUT_MESSAGES = 50
MAX_INPUT_LENGTH = 100000

SKYSMART_API_URL = "https://skysmart-answers.vercel.app/get_answers/"


# ============================================================
# GLOBALS
# ============================================================

supabase: Client | None = None
telegram_app: Application | None = None

user_buffers: dict[int, list[str]] = {}
user_last_control_message: dict[int, int] = {}
user_last_texts: dict[int, list[dict]] = {}

user_last_skysmart_data: dict[int, dict] = {}


# ============================================================
# SUPABASE
# ============================================================

def init_supabase():
    global supabase

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("WARNING: Supabase environment variables are missing")
        return

    supabase = create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_KEY
    )

    print("Supabase initialized")


# ============================================================
# PASSWORDS
# ============================================================

def hash_password(password: str) -> str:
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def generate_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    part1 = "".join(
        secrets.choice(alphabet)
        for _ in range(4)
    )

    part2 = "".join(
        secrets.choice(alphabet)
        for _ in range(4)
    )

    return f"{part1}-{part2}"


def initialize_passwords():
    if supabase is None:
        return

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

            supabase.table("bot_passwords").insert({
                "password_hash": hash_password(password),
                "password_text": password,
                "used": False,
            }).execute()

            current_count += 1

        print("Passwords initialized")

    except Exception as e:
        print("Password initialization error:", e)


def is_authorized(user_id: int) -> bool:
    if supabase is None:
        return False

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

        return bool(
            result.data[0].get("authorized", False)
        )

    except Exception as e:
        print("Authorization check error:", e)
        return False


def create_user_if_needed(user_id: int):
    if supabase is None:
        return

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
        print("Create user error:", e)


def use_password(password: str, user_id: int) -> bool:
    if supabase is None:
        return False

    password = password.strip().upper()

    try:
        password_hash = hash_password(password)

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

        supabase.table("bot_passwords").update({
            "used": True,
            "used_by": user_id,
            "used_at": "now()",
        }).eq(
            "id",
            password_row["id"]
        ).execute()

        create_user_if_needed(user_id)

        supabase.table("bot_users").update({
            "authorized": True,
        }).eq(
            "telegram_id",
            user_id
        ).execute()

        return True

    except Exception as e:
        print("Use password error:", e)
        return False


def get_unused_passwords() -> list[str]:
    if supabase is None:
        return []

    try:
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

    except Exception as e:
        print("Get passwords error:", e)
        return []


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_input_text(text: str) -> str:
    if not text:
        return ""

    text = str(text)

    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


def clean_answer(answer: str) -> str:
    if not answer:
        return ""

    answer = str(answer)

    answer = re.sub(
        r"^\s*(ответ|answer)\s*:\s*",
        "",
        answer,
        flags=re.IGNORECASE
    )

    answer = re.sub(
        r"Получил сообщение\s*",
        "",
        answer,
        flags=re.IGNORECASE
    )

    return answer.strip()


def clean_question(question: str) -> str:
    if not question:
        return ""

    question = str(question)

    question = re.sub(
        r"^\s*(вопрос|question)\s*\d*\s*[:.)-]?\s*",
        "",
        question,
        flags=re.IGNORECASE
    )

    question = re.sub(
        r"Получил сообщение\s*",
        "",
        question,
        flags=re.IGNORECASE
    )

    return question.strip()


# ============================================================
# TELEGRAM MESSAGE SPLITTING
# ============================================================

def split_by_telegram_messages(text: str) -> list[str]:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    chunks = []

    while text:
        chunk = text[:MAX_MESSAGE_LENGTH]

        if len(text) > MAX_MESSAGE_LENGTH:
            pos = chunk.rfind("\n")

            if pos > 500:
                chunk = chunk[:pos]

        chunks.append(chunk)
        text = text[len(chunk):]

    return chunks


async def send_long_message(
    message,
    text: str,
    reply_markup=None
):
    chunks = split_by_telegram_messages(text)

    for i, chunk in enumerate(chunks):
        await message.reply_text(
            chunk,
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
            disable_web_page_preview=True
        )


# ============================================================
# ORDINARY TASK PARSER
# ============================================================

def normalize_line(line: str) -> str:
    line = line.strip()

    line = re.sub(
        r"\s+",
        " ",
        line
    )

    return line


def is_probable_option_line(line: str) -> bool:
    line = normalize_line(line)

    patterns = [
        r"^[A-HА-Н]\s*[\)\.:]\s*.+",
        r"^[0-9]{1,2}\s*[\)\.:]\s*.+",
        r"^[•●○]\s*.+",
        r"^[-–—]\s*.+",
    ]

    return any(
        re.match(pattern, line, re.IGNORECASE)
        for pattern in patterns
    )


def remove_choice_options(question: str) -> str:
    lines = question.splitlines()

    result = []

    for line in lines:
        if is_probable_option_line(line):
            continue

        result.append(line)

    return "\n".join(result).strip()


def parse_single_message(text: str) -> list[dict]:
    text = clean_input_text(text)

    if not text:
        return []

    pattern = re.compile(
        r"(?:Вопрос|Question)\s*"
        r"(\d+)"
        r"\s*[:.)-]?\s*"
        r"(.*?)"
        r"(?:Ответ|Answer)\s*[:.)-]?\s*"
        r"(.*?)(?="
        r"(?:Вопрос|Question)\s*\d+\s*[:.)-]?"
        r"|$)",
        re.IGNORECASE | re.DOTALL
    )

    matches = pattern.findall(text)

    questions = []

    for number, question, answer in matches:
        question = clean_question(question)
        answer = clean_answer(answer)

        question = remove_choice_options(question)

        if not question and not answer:
            continue

        questions.append({
            "number": number.strip(),
            "question": question.strip(),
            "answer": answer.strip(),
        })

    return questions


def parse_questions(messages: list[str]) -> list[dict]:
    all_questions = []

    for message in messages:
        parsed = parse_single_message(message)
        all_questions.extend(parsed)

    return normalize_questions(all_questions)


def normalize_questions(questions: list[dict]) -> list[dict]:
    result = []

    seen = set()

    for item in questions:
        number = str(
            item.get("number", "")
        ).strip()

        question = clean_question(
            item.get("question", "")
        )

        answer = clean_answer(
            item.get("answer", "")
        )

        key = (
            number,
            question,
            answer
        )

        if key in seen:
            continue

        seen.add(key)

        result.append({
            "number": number,
            "question": question,
            "answer": answer,
        })

    return result


# ============================================================
# LATEX -> READABLE
# ============================================================

SUPERSCRIPT_MAP = {
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
    "a": "ᵃ",
    "b": "ᵇ",
    "c": "ᶜ",
    "d": "ᵈ",
    "e": "ᵉ",
    "f": "ᶠ",
    "g": "ᵍ",
    "h": "ʰ",
    "j": "ʲ",
    "k": "ᵏ",
    "l": "ˡ",
    "m": "ᵐ",
    "o": "ᵒ",
    "p": "ᵖ",
    "r": "ʳ",
    "s": "ˢ",
    "t": "ᵗ",
    "u": "ᵘ",
    "v": "ᵛ",
    "w": "ʷ",
    "x": "ˣ",
    "y": "ʸ",
    "z": "ᶻ",
}


def convert_superscript(text: str) -> str:

    def braced(match):
        content = match.group(1)

        result = ""

        for char in content:
            result += SUPERSCRIPT_MAP.get(
                char,
                char
            )

        return result

    text = re.sub(
        r"\^\{([^{}]*)\}",
        braced,
        text
    )

    def simple(match):
        char = match.group(1)

        return SUPERSCRIPT_MAP.get(
            char,
            "^" + char
        )

    text = re.sub(
        r"\^([A-Za-z0-9+\-=()])",
        simple,
        text
    )

    return text


def extract_braced_content(
    text: str,
    start: int
):
    if start >= len(text) or text[start] != "{":
        return None, start

    depth = 0

    for i in range(start, len(text)):
        char = text[i]

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                return (
                    text[start + 1:i],
                    i + 1
                )

    return None, start


def replace_latex_fractions(text: str) -> str:

    commands = [
        r"\dfrac",
        r"\tfrac",
        r"\frac",
    ]

    changed = True

    while changed:
        changed = False

        for command in commands:
            pos = text.find(command)

            while pos != -1:

                start = pos + len(command)

                while (
                    start < len(text)
                    and text[start].isspace()
                ):
                    start += 1

                if (
                    start >= len(text)
                    or text[start] != "{"
                ):
                    pos = text.find(
                        command,
                        pos + len(command)
                    )
                    continue

                numerator, next_pos = extract_braced_content(
                    text,
                    start
                )

                if numerator is None:
                    pos = text.find(
                        command,
                        pos + len(command)
                    )
                    continue

                start2 = next_pos

                while (
                    start2 < len(text)
                    and text[start2].isspace()
                ):
                    start2 += 1

                if (
                    start2 >= len(text)
                    or text[start2] != "{"
                ):
                    pos = text.find(
                        command,
                        pos + len(command)
                    )
                    continue

                denominator, end_pos = extract_braced_content(
                    text,
                    start2
                )

                if denominator is None:
                    pos = text.find(
                        command,
                        pos + len(command)
                    )
                    continue

                numerator = latex_to_readable(
                    numerator
                )

                denominator = latex_to_readable(
                    denominator
                )

                replacement = (
                    f"({numerator}/{denominator})"
                )

                text = (
                    text[:pos]
                    + replacement
                    + text[end_pos:]
                )

                changed = True

                pos = text.find(
                    command,
                    pos + len(replacement)
                )

    return text


def replace_latex_sqrt(text: str) -> str:

    command = r"\sqrt"

    pos = text.find(command)

    while pos != -1:

        start = pos + len(command)

        while (
            start < len(text)
            and text[start].isspace()
        ):
            start += 1

        if (
            start >= len(text)
            or text[start] != "{"
        ):
            pos = text.find(
                command,
                pos + len(command)
            )
            continue

        content, end_pos = extract_braced_content(
            text,
            start
        )

        if content is None:
            break

        content = latex_to_readable(content)

        replacement = f"√({content})"

        text = (
            text[:pos]
            + replacement
            + text[end_pos:]
        )

        pos = text.find(
            command,
            pos + len(replacement)
        )

    return text


def latex_to_readable(text: str) -> str:

    if text is None:
        return ""

    text = str(text)

    # --------------------------------------------------------
    # IMPORTANT:
    # Skysmart sometimes sends comparison signs as words.
    # gt -> >
    # lt -> <
    # ge -> >=
    # le -> <=
    # neq -> !=
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

    # HTML entities
    text = (
        text
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&ge;", "≥")
        .replace("&le;", "≤")
        .replace("&amp;", "&")
    )

    replacements = {

        # Infinity / number sets
        r"\infty": "∞",

        r"\mathbb{R}": "ℝ",
        r"\mathbb{N}": "ℕ",
        r"\mathbb{Z}": "ℤ",
        r"\mathbb{Q}": "ℚ",

        r"\mathbb R": "ℝ",
        r"\mathbb N": "ℕ",
        r"\mathbb Z": "ℤ",
        r"\mathbb Q": "ℚ",

        r"\R": "ℝ",
        r"\N": "ℕ",
        r"\Z": "ℤ",
        r"\Q": "ℚ",

        # Comparison
        r"\leq": "≤",
        r"\le": "≤",

        r"\geq": "≥",
        r"\ge": "≥",

        r"\neq": "≠",
        r"\ne": "≠",

        # Math symbols
        r"\pm": "±",
        r"\mp": "∓",
        r"\times": "×",
        r"\cdot": "·",
        r"\div": "÷",

        # Sets
        r"\in": "∈",
        r"\notin": "∉",
        r"\subset": "⊂",
        r"\subseteq": "⊆",
        r"\cup": "∪",
        r"\cap": "∩",

        # Logic
        r"\forall": "∀",
        r"\exists": "∃",

        # Arrows
        r"\rightarrow": "→",
        r"\to": "→",
        r"\leftarrow": "←",
        r"\leftrightarrow": "↔",

        # Geometry
        r"\angle": "∠",
        r"\degree": "°",

        # Other
        r"\emptyset": "∅",
    }

    # Replace longest commands first
    for old, new in sorted(
        replacements.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):
        text = text.replace(old, new)

    # --------------------------------------------------------
    # Sizing commands
    # --------------------------------------------------------

    for command in [
        r"\Bigg",
        r"\bigg",
        r"\Big",
        r"\big",
        r"\left",
        r"\right",
        r"\middle",
    ]:
        text = text.replace(command, "")

    # --------------------------------------------------------
    # LaTeX spacing
    # --------------------------------------------------------

    for spacing in [
        r"\,",
        r"\;",
        r"\:",
        r"\!",
        r"\ ",
        "~",
    ]:
        text = text.replace(spacing, "")

    # --------------------------------------------------------
    # Fractions
    # --------------------------------------------------------

    text = replace_latex_fractions(text)

    # --------------------------------------------------------
    # Square roots
    # --------------------------------------------------------

    text = replace_latex_sqrt(text)

    # --------------------------------------------------------
    # Text commands
    # --------------------------------------------------------

    text = re.sub(
        r"\\text\s*\{([^{}]*)\}",
        r"\1",
        text
    )

    text = re.sub(
        r"\\mathrm\s*\{([^{}]*)\}",
        r"\1",
        text
    )

    text = re.sub(
        r"\\operatorname\s*\{([^{}]*)\}",
        r"\1",
        text
    )

    # --------------------------------------------------------
    # Escaped braces
    # --------------------------------------------------------

    text = text.replace(
        r"\{",
        "{"
    )

    text = text.replace(
        r"\}",
        "}"
    )

    # Remaining braces usually represent grouping.
    text = text.replace(
        "{",
        "("
    ).replace(
        "}",
        ")"
    )

    # --------------------------------------------------------
    # Powers
    # --------------------------------------------------------

    text = convert_superscript(text)

    # --------------------------------------------------------
    # Remaining LaTeX commands
    # --------------------------------------------------------

    text = re.sub(
        r"\\([A-Za-z]+)",
        r"\1",
        text
    )

    # Multiplication
    text = text.replace(
        "*",
        "×"
    )

    # Normalize minus
    text = text.replace(
        "−",
        "-"
    )

    # --------------------------------------------------------
    # Spaces around operators
    # --------------------------------------------------------

    text = re.sub(
        r"\s*([=<>])\s*",
        r" \1 ",
        text
    )

    text = re.sub(
        r"\s*([+])\s*",
        r" \1 ",
        text
    )

    # Don't destroy negative numbers
    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    text = re.sub(
        r"\n[ \t]+",
        "\n",
        text
    )

    text = re.sub(
        r"[ \t]+\n",
        "\n",
        text
    )

    # Fix spaces after opening brackets
    text = re.sub(
        r"\(\s+",
        "(",
        text
    )

    text = re.sub(
        r"\s+\)",
        ")",
        text
    )

    return text.strip()


def clean_skysmart_text(text) -> str:

    if text is None:
        return ""

    text = str(text)

    # Decode HTML entities first
    text = html.unescape(text)

    text = latex_to_readable(text)

    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


# ============================================================
# SKYSMART
# ============================================================

SKYSMART_URL_RE = re.compile(
    r"https?://edu\.skysmart\.ru/student/([A-Za-z0-9_-]+)",
    re.IGNORECASE
)


def extract_skysmart_room(text: str):
    match = SKYSMART_URL_RE.search(text)

    if not match:
        return None

    return match.group(1)


def get_skysmart_data(room_name: str):
    try:
        response = requests.post(
            SKYSMART_API_URL,
            json={
                "roomName": room_name
            },
            timeout=30
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:
        print(
            "Skysmart API error:",
            repr(e)
        )

        return None


def get_skysmart_tasks(data):
    if not isinstance(data, list):
        return []

    if len(data) < 1:
        return []

    tasks = data[0]

    if not isinstance(tasks, list):
        return []

    return tasks


def get_skysmart_info(data):
    if not isinstance(data, list):
        return {}

    if len(data) < 2:
        return {}

    info = data[1]

    if not isinstance(info, dict):
        return {}

    return info


def get_task_question(task: dict) -> str:

    for key in [
        "question",
        "text",
        "title",
        "task",
        "condition",
        "content",
    ]:
        value = task.get(key)

        if value:
            return clean_skysmart_text(value)

    return ""


def get_task_answer(task: dict) -> str:

    for key in [
        "answer",
        "answers",
        "correctAnswer",
        "correct_answer",
        "solution",
        "result",
    ]:
        value = task.get(key)

        if value is not None:
            if isinstance(value, list):
                return ", ".join(
                    clean_skysmart_text(x)
                    for x in value
                )

            if isinstance(value, dict):
                return clean_skysmart_text(
                    str(value)
                )

            return clean_skysmart_text(value)

    return ""


def get_task_number(
    task: dict,
    index: int
) -> str:

    for key in [
        "number",
        "taskNumber",
        "task_number",
        "index",
        "id",
    ]:
        value = task.get(key)

        if value is not None:
            return str(value)

    return str(index + 1)


def get_skysmart_header(info: dict) -> str:

    title = clean_skysmart_text(
        info.get("title", "")
    )

    module = clean_skysmart_text(
        info.get("module", "")
        or info.get("lesson", "")
    )

    subject = clean_skysmart_text(
        info.get("subject", "")
    )

    teacher = clean_skysmart_text(
        info.get("teacher", "")
    )

    parts = []

    if title:
        parts.append(
            f"📚 {title}"
        )

    if module:
        parts.append(
            f"📖 {module}"
        )

    if subject:
        parts.append(
            f"📌 {subject}"
        )

    if teacher:
        parts.append(
            f"👨‍🏫 {teacher}"
        )

    return "\n".join(parts)


def prepare_skysmart_tasks(data) -> list[dict]:

    tasks = get_skysmart_tasks(data)

    result = []

    for index, task in enumerate(tasks):

        if not isinstance(task, dict):
            continue

        number = get_task_number(
            task,
            index
        )

        question = get_task_question(
            task
        )

        answer = get_task_answer(
            task
        )

        result.append({
            "number": number,
            "question": question,
            "answer": answer,
        })

    return result


# ============================================================
# SKYSMART FORMATS
# ============================================================

def make_skysmart_mobile(
    data
) -> str:

    info = get_skysmart_info(data)
    tasks = prepare_skysmart_tasks(data)

    parts = []

    header = get_skysmart_header(info)

    if header:
        parts.append(header)

    parts.append("")

    for task in tasks:

        number = task["number"]
        question = task["question"]
        answer = task["answer"]

        parts.append(
            f"❓ <b>{html.escape(number)}.</b>"
        )

        if question:
            parts.append(
                html.escape(question)
            )

        parts.append("")

        parts.append(
            f"✅ <b>Ответ:</b> "
            f"{html.escape(answer)}"
        )

        parts.append("")

    return "\n".join(parts).strip()


def make_skysmart_list(
    data
) -> str:

    info = get_skysmart_info(data)
    tasks = prepare_skysmart_tasks(data)

    parts = []

    header = get_skysmart_header(info)

    if header:
        parts.append(header)
        parts.append("")

    for task in tasks:

        number = html.escape(
            task["number"]
        )

        answer = html.escape(
            task["answer"]
        )

        parts.append(
            f"<b>{number}.</b> {answer}"
        )

    return "\n".join(parts)


def make_skysmart_compact(
    data
) -> str:

    tasks = prepare_skysmart_tasks(data)

    answers = []

    for task in tasks:

        answer = task["answer"]

        if answer:
            answers.append(
                answer
            )

    return ", ".join(
        answers
    )


# ============================================================
# ORDINARY FORMATS
# ============================================================

def make_mobile(
    questions: list[dict]
) -> str:

    parts = []

    for item in questions:

        number = html.escape(
            item["number"]
        )

        question = html.escape(
            item["question"]
        )

        answer = html.escape(
            item["answer"]
        )

        parts.append(
            f"❓ <b>{number}.</b>"
        )

        if question:
            parts.append(
                question
            )

        parts.append("")

        parts.append(
            f"✅ <b>Ответ:</b> {answer}"
        )

        parts.append("")

    return "\n".join(parts).strip()


def make_list(
    questions: list[dict]
) -> str:

    parts = []

    for item in questions:

        number = html.escape(
            item["number"]
        )

        answer = html.escape(
            item["answer"]
        )

        parts.append(
            f"<b>{number}.</b> {answer}"
        )

    return "\n".join(parts)


def make_compact(
    questions: list[dict]
) -> str:

    answers = []

    for item in questions:

        answer = item["answer"].strip()

        if answer:
            answers.append(
                answer
            )

    return ", ".join(
        answers
    )


# ============================================================
# KEYBOARDS
# ============================================================

def result_keyboard() -> InlineKeyboardMarkup:

    keyboard = [
        [
            InlineKeyboardButton(
                "📱 Удобно",
                callback_data="format_mobile"
            ),
            InlineKeyboardButton(
                "📝 Список",
                callback_data="format_list"
            ),
        ],
        [
            InlineKeyboardButton(
                "⚡ Только ответы",
                callback_data="format_compact"
            ),
            InlineKeyboardButton(
                "🔄 Заново",
                callback_data="format_repeat"
            ),
        ],
    ]

    return InlineKeyboardMarkup(
        keyboard
    )


def finish_keyboard() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏹ Завершить ввод",
                callback_data="finish_input"
            )
        ]
    ])


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
            "Отправляйте задания сообщениями.\n"
            "Когда закончите — нажмите "
            "«⏹ Завершить ввод»."
        )

        return

    await update.message.reply_text(
        "🔐 Для использования бота нужен пароль.\n\n"
        "Отправьте пароль в формате:\n"
        "<code>XXXX-XXXX</code>",
        parse_mode="HTML"
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
        f"🆔 Ваш Telegram ID:\n"
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

    if str(user.id) != str(OWNER_ID):

        await update.message.reply_text(
            "⛔ Недостаточно прав."
        )

        return

    passwords = get_unused_passwords()

    if not passwords:

        await update.message.reply_text(
            "Паролей нет."
        )

        return

    text = "🔑 <b>Свободные пароли:</b>\n\n"

    for password in passwords:
        text += (
            f"<code>{html.escape(password)}</code>\n"
        )

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


# ============================================================
# PASSWORD / NORMAL TEXT / SKYSMART URL
# ============================================================

async def text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.message is None:
        return

    user = update.effective_user

    if user is None:
        return

    user_id = user.id
    text = update.message.text or ""

    text = text.strip()

    if not text:
        return

    # --------------------------------------------------------
    # Authorization
    # --------------------------------------------------------

    create_user_if_needed(
        user_id
    )

    if not is_authorized(user_id):

        if use_password(
            text,
            user_id
        ):

            await update.message.reply_text(
                "✅ Пароль принят!\n\n"
                "Доступ открыт.\n"
                "Теперь отправляйте задания."
            )

        else:

            await update.message.reply_text(
                "❌ Неверный или уже использованный пароль."
            )

        return

    # --------------------------------------------------------
    # Skysmart URL
    # --------------------------------------------------------

    room_name = extract_skysmart_room(
        text
    )

    if room_name:

        processing_message = (
            await update.message.reply_text(
                "⏳ Получаю задания из Skysmart..."
            )
        )

        data = await asyncio.to_thread(
            get_skysmart_data,
            room_name
        )

        if not data:

            await processing_message.edit_text(
                "❌ Не удалось получить данные Skysmart."
            )

            return

        tasks = prepare_skysmart_tasks(
            data
        )

        if not tasks:

            await processing_message.edit_text(
                "❌ В ответе Skysmart не найдено заданий."
            )

            return

        user_last_skysmart_data[
            user_id
        ] = data

        output = make_skysmart_mobile(
            data
        )

        await processing_message.edit_text(
            output[:MAX_MESSAGE_LENGTH],
            parse_mode="HTML"
        )

        await update.message.reply_text(
            "Выберите формат:",
            reply_markup=result_keyboard()
        )

        return

    # --------------------------------------------------------
    # Ordinary task input
    # --------------------------------------------------------

    if user_id not in user_buffers:
        user_buffers[user_id] = []

    current_size = sum(
        len(x)
        for x in user_buffers[user_id]
    )

    if (
        len(user_buffers[user_id])
        >= MAX_INPUT_MESSAGES
    ):

        await update.message.reply_text(
            "⚠️ Слишком много сообщений.\n"
            "Нажмите «⏹ Завершить ввод»."
        )

        return

    if current_size + len(text) > MAX_INPUT_LENGTH:

        await update.message.reply_text(
            "⚠️ Слишком большой объём текста."
        )

        return

    user_buffers[user_id].append(
        text
    )

    # Delete previous control message
    old_message_id = user_last_control_message.get(
        user_id
    )

    if old_message_id:

        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=old_message_id
            )
        except Exception:
            pass

    control_message = await update.message.reply_text(
        "📥 Сообщение получено.\n"
        f"Всего сообщений: "
        f"{len(user_buffers[user_id])}\n\n"
        "Когда закончите отправлять задания, "
        "нажмите кнопку ниже.",
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

        await query.message.edit_text(
            "⚠️ Вы ещё ничего не отправили."
        )

        return

    questions = parse_questions(
        messages
    )

    user_last_texts[
        user_id
    ] = questions

    user_buffers[
        user_id
    ] = []

    if not questions:

        await query.message.edit_text(
            "❌ Не удалось распознать задания.\n\n"
            "Проверьте, что текст содержит пары "
            "«Вопрос ... Ответ ...»."
        )

        return

    output = make_mobile(
        questions
    )

    await query.message.edit_text(
        output[:MAX_MESSAGE_LENGTH],
        parse_mode="HTML"
    )

    await query.message.reply_text(
        "Выберите формат:",
        reply_markup=result_keyboard()
    )


# ============================================================
# RESULT BUTTONS
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
    # Skysmart
    # --------------------------------------------------------

    if user_id in user_last_skysmart_data:

        data = user_last_skysmart_data[
            user_id
        ]

        if callback == "format_mobile":

            output = make_skysmart_mobile(
                data
            )

        elif callback == "format_list":

            output = make_skysmart_list(
                data
            )

        elif callback == "format_compact":

            output = make_skysmart_compact(
                data
            )

        elif callback == "format_repeat":

            output = make_skysmart_mobile(
                data
            )

        else:
            return

        await send_long_message(
            query.message,
            output
        )

        return

    # --------------------------------------------------------
    # Ordinary tasks
    # --------------------------------------------------------

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

    elif callback == "format_list":

        output = make_list(
            questions
        )

    elif callback == "format_compact":

        output = make_compact(
            questions
        )

    elif callback == "format_repeat":

        output = make_mobile(
            questions
        )

    else:
        return

    await send_long_message(
        query.message,
        output
    )


# ============================================================
# STARLETTE
# ============================================================

async def index(
    request: Request
):
    return PlainTextResponse(
        "Bot is running."
    )


async def health(
    request: Request
):
    return PlainTextResponse(
        "OK"
    )


async def telegram_webhook(
    request: Request
):

    global telegram_app

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            telegram_app.bot
        )

        await telegram_app.update_queue.put(
            update
        )

        return Response(
            content="OK",
            status_code=200
        )

    except Exception as e:

        print(
            "Webhook error:",
            repr(e)
        )

        return Response(
            content="ERROR",
            status_code=500
        )


routes = [
    Route(
        "/",
        index,
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
    ),
]


app = Starlette(
    routes=routes
)


# ============================================================
# MAIN
# ============================================================

async def main():

    global telegram_app

    print("Starting bot...")

    init_supabase()

    initialize_passwords()

    telegram_app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .updater(None)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "id",
            id_command
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "keys",
            keys_command
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            finish_input,
            pattern=r"^finish_input$"
        )
    )

    telegram_app.add_handler(
        CallbackQueryHandler(
            result_callback,
            pattern=r"^format_"
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_message
        )
    )

    await telegram_app.initialize()

    await telegram_app.start()

    webhook_url = (
        f"{RENDER_URL}/telegram"
    )

    print(
        "Setting webhook:",
        webhook_url
    )

    await telegram_app.bot.set_webhook(
        url=webhook_url
    )

    print(
        "Bot started successfully."
    )

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(
        config
    )

    await server.serve()


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        pass
