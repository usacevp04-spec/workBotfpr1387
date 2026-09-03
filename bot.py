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


# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

OWNER_ID = os.getenv("OWNER_ID")

PORT = int(os.environ.get("PORT", 10000))

MAX_MESSAGE_LENGTH = 3900
MAX_INPUT_MESSAGES = 50
MAX_INPUT_LENGTH = 100000

SKYSMART_API_URL = (
    "https://skysmart-answers.vercel.app/get_answers/"
)


# =========================================================
# SUPABASE
# =========================================================

supabase: Client | None = None


def get_supabase():

    global supabase

    if supabase is None:

        if not SUPABASE_URL:
            raise ValueError(
                "Не задана переменная SUPABASE_URL"
            )

        if not SUPABASE_SERVICE_KEY:
            raise ValueError(
                "Не задана переменная SUPABASE_SERVICE_KEY"
            )

        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_SERVICE_KEY
        )

    return supabase


# =========================================================
# ПАМЯТЬ
# =========================================================

user_buffers = {}
user_last_control_message = {}
user_last_texts = {}
user_last_questions = {}

user_last_skysmart_data = {}
user_last_skysmart_room = {}


# =========================================================
# ПАРОЛИ
# =========================================================

def generate_password():

    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ"
        "23456789"
    )

    first = "".join(
        secrets.choice(alphabet)
        for _ in range(4)
    )

    second = "".join(
        secrets.choice(alphabet)
        for _ in range(4)
    )

    return f"{first}-{second}"


def hash_password(password):

    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def initialize_passwords():

    db = get_supabase()

    try:

        result = (
            db.table("bot_passwords")
            .select("id,password_text")
            .execute()
        )

        existing = result.data or []

        if len(existing) >= 10:

            print("Пароли уже существуют.")

            return

        existing_texts = {
            row.get("password_text")
            for row in existing
            if row.get("password_text")
        }

        needed = 10 - len(existing)

        generated = []

        while len(generated) < needed:

            password = generate_password()

            if password in existing_texts:
                continue

            if password in generated:
                continue

            generated.append(password)

        for password in generated:

            db.table("bot_passwords").insert({

                "password_hash": hash_password(password),
                "password_text": password,
                "used": False,
                "used_by": None,
                "used_at": None,

            }).execute()

        print("")
        print("=" * 50)
        print("🔐 НОВЫЕ ПАРОЛИ")
        print("=" * 50)

        for password in generated:
            print(password)

        print("=" * 50)

    except Exception as error:

        print(
            "Ошибка создания паролей:",
            error
        )


# =========================================================
# АВТОРИЗАЦИЯ
# =========================================================

def is_authorized(telegram_id):

    db = get_supabase()

    try:

        result = (
            db.table("bot_users")
            .select("authorized")
            .eq("telegram_id", telegram_id)
            .limit(1)
            .execute()
        )

        if not result.data:
            return False

        return bool(
            result.data[0].get(
                "authorized",
                False
            )
        )

    except Exception as error:

        print(
            "Ошибка проверки авторизации:",
            error
        )

        return False


def create_user_if_needed(telegram_id):

    db = get_supabase()

    try:

        result = (
            db.table("bot_users")
            .select("telegram_id")
            .eq("telegram_id", telegram_id)
            .limit(1)
            .execute()
        )

        if result.data:
            return

        db.table("bot_users").insert({

            "telegram_id": telegram_id,
            "authorized": False

        }).execute()

    except Exception as error:

        print(
            "Ошибка создания пользователя:",
            error
        )


def use_password(password, telegram_id):

    db = get_supabase()

    password = password.strip().upper()

    password_hash = hash_password(password)

    try:

        result = (
            db.table("bot_passwords")
            .select("id,used,password_text")
            .eq("password_hash", password_hash)
            .limit(1)
            .execute()
        )

        if not result.data:
            return False, "wrong"

        row = result.data[0]

        if row.get("used"):
            return False, "used"

        db.table("bot_passwords").update({

            "used": True,
            "used_by": telegram_id,
            "used_at": "now()",

        }).eq(
            "id",
            row["id"]
        ).execute()

        db.table("bot_users").update({

            "authorized": True

        }).eq(
            "telegram_id",
            telegram_id
        ).execute()

        return True, "success"

    except Exception as error:

        print(
            "Ошибка проверки пароля:",
            error
        )

        return False, "error"


# =========================================================
# СТАТИСТИКА ПАРОЛЕЙ
# =========================================================

def get_password_stats():

    db = get_supabase()

    try:

        result = (
            db.table("bot_passwords")
            .select("password_text,used,used_by")
            .execute()
        )

        rows = result.data or []

        total = len(rows)

        used = sum(
            1
            for row in rows
            if row.get("used")
        )

        free = total - used

        return rows, total, used, free

    except Exception as error:

        print(
            "Ошибка получения паролей:",
            error
        )

        return [], 0, 0, 0


def make_password_list():

    rows, total, used, free = get_password_stats()

    result = (
        "🔐 <b>ПАРОЛИ ДОСТУПА</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"Использовано: <b>{used}</b>\n"
        f"Свободно: <b>{free}</b>\n\n"
    )

    for index, row in enumerate(rows, start=1):

        password = row.get(
            "password_text",
            "???"
        )

        if row.get("used"):

            used_by = row.get("used_by")

            result += (
                f"🔴 {index}. "
                f"<s>{html.escape(str(password))}</s>"
                " — использован"
            )

            if used_by:
                result += f" ({used_by})"

            result += "\n"

        else:

            result += (
                f"🟢 {index}. "
                f"<code>{html.escape(str(password))}</code>"
                " — свободен\n"
            )

    return result


def is_owner(telegram_id):

    if not OWNER_ID:
        return False

    return str(telegram_id) == str(OWNER_ID)


# =========================================================
# КНОПКА ЗАВЕРШЕНИЯ
# =========================================================

def get_finish_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "⏹ Завершить ввод",
                callback_data="finish_input"
            )
        ]

    ])


# =========================================================
# КНОПКИ РЕЗУЛЬТАТА
# =========================================================

def get_result_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "📱 Удобно",
                callback_data="mobile"
            ),
            InlineKeyboardButton(
                "📝 Список",
                callback_data="list"
            ),
        ],

        [
            InlineKeyboardButton(
                "⚡ Только ответы",
                callback_data="compact"
            ),
            InlineKeyboardButton(
                "🔄 Заново",
                callback_data="repeat"
            ),
        ],

    ])


# =========================================================
# SKYSMART
# =========================================================

def extract_skysmart_room(text):

    if not text:
        return None

    match = re.search(
        r"https?://edu\.skysmart\.ru/student/"
        r"([^/?#\s]+)",
        text,
        flags=re.IGNORECASE
    )

    if not match:
        return None

    room_name = match.group(1).strip()

    return room_name if room_name else None


def get_skysmart_answers(room_name):

    response = requests.post(
        SKYSMART_API_URL,
        json={
            "roomName": room_name
        },
        timeout=30
    )

    response.raise_for_status()

    return response.json()


def clean_skysmart_text(text):

    if text is None:
        return ""

    text = str(text)

    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    lines = [
        line.rstrip()
        for line in text.split("\n")
    ]

    return "\n".join(lines).strip()


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

    return info if isinstance(info, dict) else {}


def make_skysmart_header(data):

    info = get_skysmart_info(data)

    result = []

    title = clean_skysmart_text(
        info.get("title", "Задание")
    )

    result.append(
        f"🎓 <b>{html.escape(title)}</b>"
    )

    meta = info.get("meta", {})

    if not isinstance(meta, dict):
        return "\n".join(result)

    path = meta.get("path", {})

    if isinstance(path, dict):

        module = path.get("module", {})

        if isinstance(module, dict):

            module_title = clean_skysmart_text(
                module.get("title")
            )

            if module_title:

                result.append(
                    "📖 <b>Модуль:</b> "
                    f"{html.escape(module_title)}"
                )

            lesson = module.get("lesson", {})

            if isinstance(lesson, dict):

                lesson_title = clean_skysmart_text(
                    lesson.get("title")
                )

                if lesson_title:

                    result.append(
                        "📘 <b>Урок:</b> "
                        f"{html.escape(lesson_title)}"
                    )

    subject = meta.get("subject", {})

    if isinstance(subject, dict):

        subject_title = clean_skysmart_text(
            subject.get("title")
        )

        if subject_title:

            result.append(
                "📐 <b>Предмет:</b> "
                f"{html.escape(subject_title)}"
            )

    teacher = meta.get("teacher", {})

    if isinstance(teacher, dict):

        teacher_name = clean_skysmart_text(
            teacher.get("name")
        )

        teacher_surname = clean_skysmart_text(
            teacher.get("surname")
        )

        teacher_full_name = " ".join(
            part
            for part in [
                teacher_name,
                teacher_surname
            ]
            if part
        )

        if teacher_full_name:

            result.append(
                "👨‍🏫 <b>Преподаватель:</b> "
                f"{html.escape(teacher_full_name)}"
            )

    return "\n".join(result)


def get_skysmart_clean_answers(task):

    answers = task.get("answers", [])

    if not isinstance(answers, list):
        return []

    result = []

    for answer in answers:

        answer_text = clean_skysmart_text(answer)

        if answer_text:
            result.append(answer_text)

    return result


def make_skysmart_mobile(data):

    tasks = get_skysmart_tasks(data)

    if not tasks:
        return "❌ Не удалось найти задания и ответы."

    parts = [
        make_skysmart_header(data)
    ]

    for task in tasks:

        if not isinstance(task, dict):
            continue

        task_number = task.get("task_number", "")

        question_title = clean_skysmart_text(
            task.get("question")
        )

        full_question = clean_skysmart_text(
            task.get("full_question")
        )

        answers = get_skysmart_clean_answers(task)

        answer_text = ", ".join(answers)

        block = (
            f"🧩 <b>ЗАДАНИЕ "
            f"{html.escape(str(task_number))}</b>"
        )

        if question_title:

            block += (
                "\n\n"
                f"📝 <b>{html.escape(question_title)}</b>"
            )

        if full_question:

            block += (
                "\n\n"
                f"{html.escape(full_question)}"
            )

        block += (
            "\n\n"
            "💬 <b>ОТВЕТ</b>\n"
        )

        block += (
            html.escape(answer_text)
            if answer_text
            else "Ответ не найден"
        )

        parts.append(block)

    return "\n\n━━━━━━━━━━━━━━━━\n\n".join(parts)


def make_skysmart_list(data):

    tasks = get_skysmart_tasks(data)

    if not tasks:
        return "❌ Не удалось найти задания и ответы."

    parts = [
        make_skysmart_header(data)
    ]

    for task in tasks:

        if not isinstance(task, dict):
            continue

        task_number = task.get("task_number", "")

        question_title = clean_skysmart_text(
            task.get("question")
        )

        full_question = clean_skysmart_text(
            task.get("full_question")
        )

        answers = get_skysmart_clean_answers(task)

        answer_text = ", ".join(answers)

        question_text = (
            full_question
            if full_question
            else question_title
        )

        question_text = re.sub(
            r"\s+",
            " ",
            question_text
        ).strip()

        parts.append(
            f"🧩 <b>{html.escape(str(task_number))}.</b> "
            f"{html.escape(question_text)}\n\n"
            f"💬 {html.escape(answer_text)}"
        )

    return "\n\n".join(parts)


def make_skysmart_compact(data):

    tasks = get_skysmart_tasks(data)

    if not tasks:
        return "❌ Не удалось найти ответы."

    parts = []

    for task in tasks:

        if not isinstance(task, dict):
            continue

        task_number = task.get("task_number", "")

        answers = get_skysmart_clean_answers(task)

        answer_text = ", ".join(answers)

        parts.append(
            f"🧩 <b>{html.escape(str(task_number))}.</b>\n"
            f"{html.escape(answer_text)}"
        )

    return "\n\n".join(parts)


# =========================================================
# ОЧИСТКА ОБЫЧНОГО ТЕКСТА
# =========================================================

def remove_links(text):

    if not text:
        return ""

    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        text
    )

    text = re.sub(
        r"https?://\S+",
        "",
        text
    )

    return text


def clean_input_text(text):

    if not text:
        return ""

    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    text = re.sub(
        r"Получил\s+сообщение\s*:?",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = remove_links(text)

    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("`", "")

    text = re.sub(
        r"^\s*\[\d{1,2}\.\d{1,2}\.\d{4}"
        r"\s+\d{1,2}:\d{2}\]"
        r"\s*[^:\n]{0,100}:\s*",
        "",
        text,
        flags=re.MULTILINE
    )

    lines = [
        line.rstrip()
        for line in text.split("\n")
    ]

    text = "\n".join(lines)

    text = re.sub(
        r"\n{4,}",
        "\n\n\n",
        text
    )

    return text.strip()


def clean_answer(answer):

    if not answer:
        return ""

    answer = answer.strip()

    answer = re.sub(
        r"^(ОТВЕТ|Ответ)\s*:?\s*",
        "",
        answer,
        flags=re.IGNORECASE
    )

    answer = remove_links(answer)

    answer = answer.replace("**", "")
    answer = answer.replace("__", "")
    answer = answer.replace("`", "")

    lines = [
        line.strip()
        for line in answer.split("\n")
        if line.strip()
    ]

    return "\n".join(lines).strip()


def clean_question(question):

    if not question:
        return ""

    question = question.strip()

    question = re.sub(
        r"^\s*Вопрос\s+\d+\s*:?\s*",
        "",
        question,
        flags=re.IGNORECASE
    )

    question = re.sub(
        r"^\s*\d+\s*[\.\)]\s*",
        "",
        question
    )

    lines = [
        line.strip()
        for line in question.split("\n")
        if line.strip()
    ]

    return "\n".join(lines).strip()


# =========================================================
# TELEGRAM EXPORT
# =========================================================

def split_by_telegram_messages(text):

    pattern = re.compile(
        r"(?="
        r"\[\d{1,2}\.\d{1,2}\.\d{4}"
        r"\s+\d{1,2}:\d{2}\]"
        r")"
    )

    parts = pattern.split(text)

    return [
        part.strip()
        for part in parts
        if part.strip()
    ]


# =========================================================
# УДАЛЕНИЕ ВАРИАНТОВ
# =========================================================

def normalize_line(line):

    line = line.strip()

    line = re.sub(
        r"^\s*(?:[A-Za-zА-Яа-я]|\d{1,2})[\.\)\-:]\s*",
        "",
        line
    )

    line = re.sub(
        r"\s+",
        " ",
        line
    )

    return line.strip().lower()


def is_probable_option_line(line):

    stripped = line.strip()

    if not stripped:
        return False

    # A) текст
    # B. текст
    # 1) текст
    # 2. текст
    if re.match(
        r"^\s*(?:[A-Za-zА-Яа-я]|\d{1,2})[\.\)]\s+\S+",
        stripped
    ):
        return True

    return False


def remove_choice_options(text, answer):

    if not text:
        return ""

    lines = text.splitlines()

    while lines and not lines[0].strip():
        lines.pop(0)

    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return ""

    answer_clean = clean_answer(answer)

    answer_normalized = normalize_line(
        answer_clean
    )

    answer_index = None

    # -----------------------------------------------------
    # Ищем строку, которая совпадает с ответом
    # -----------------------------------------------------

    for index, line in enumerate(lines):

        normalized = normalize_line(line)

        if normalized == answer_normalized:
            answer_index = index

    # -----------------------------------------------------
    # Если ответ находится как вариант A) ...
    # -----------------------------------------------------

    if answer_index is None:

        for index, line in enumerate(lines):

            normalized = normalize_line(line)

            if normalized == answer_normalized:
                answer_index = index
                break

    # -----------------------------------------------------
    # Если точного совпадения нет,
    # определяем блок вариантов по последним строкам.
    # -----------------------------------------------------

    if answer_index is None:

        option_indexes = []

        for index, line in enumerate(lines):

            if is_probable_option_line(line):
                option_indexes.append(index)

        if option_indexes:

            first_option = option_indexes[0]

            # Если варианты идут подряд,
            # всё начиная с первого варианта
            # считаем вариантами.
            consecutive = True

            for i in range(
                first_option,
                len(lines)
            ):

                current = lines[i].strip()

                if not current:
                    continue

                if not is_probable_option_line(current):
                    consecutive = False
                    break

            if consecutive:

                return "\n".join(
                    lines[:first_option]
                ).strip()

    if answer_index is None:
        return text.strip()

    # -----------------------------------------------------
    # Находим начало блока вариантов.
    #
    # Поддерживает:
    # A) ...
    # B) ...
    # C) ...
    #
    # 1) ...
    # 2) ...
    # 3) ...
    #
    # и варианты без букв/цифр.
    # -----------------------------------------------------

    option_start = answer_index

    # Варианты с буквами/цифрами
    if is_probable_option_line(
        lines[answer_index]
    ):

        i = answer_index - 1

        while i >= 0:

            current = lines[i].strip()

            if not current:
                i -= 1
                continue

            if is_probable_option_line(current):
                option_start = i
                i -= 1
                continue

            break

    else:

        # Варианты без маркировки:
        # несколько коротких строк перед ответом.
        i = answer_index - 1
        candidate_count = 0

        while i >= 0:

            current = lines[i].strip()

            if not current:

                if candidate_count >= 2:
                    break

                i -= 1
                continue

            # Если строка заканчивается обычным
            # предложением — скорее всего это вопрос.
            if re.search(
                r"[.!?…]$",
                current
            ):

                break

            candidate_count += 1
            option_start = i
            i -= 1

    if option_start < answer_index:

        cleaned = lines[:option_start]

        return "\n".join(
            cleaned
        ).strip()

    return text.strip()


# =========================================================
# ПАРСИНГ ОДНОГО СООБЩЕНИЯ
# =========================================================

def parse_single_message(text):

    text = clean_input_text(text)

    if not text:
        return []

    # -----------------------------------------------------
    # Основной разделитель:
    #
    # ОТВЕТ:
    # Ответ:
    # ОТВЕТ
    # -----------------------------------------------------

    answer_pattern = re.compile(
        r"(?:^|\n)"
        r"\s*(?:\[)?ОТВЕТ(?:\])?"
        r"\s*:?\s*",
        re.IGNORECASE
    )

    matches = list(
        answer_pattern.finditer(text)
    )

    if not matches:
        return []

    answer_match = matches[-1]

    question_part = text[
        :answer_match.start()
    ].strip()

    answer_part = text[
        answer_match.end():
    ].strip()

    # -----------------------------------------------------
    # Ответ берём только как содержимое
    # после последнего ОТВЕТ.
    # -----------------------------------------------------

    answer_part = clean_answer(
        answer_part
    )

    if not answer_part:
        return []

    # -----------------------------------------------------
    # Если после ОТВЕТ внезапно осталось
    # несколько строк, первая непустая строка
    # считается самим ответом.
    # -----------------------------------------------------

    answer_lines = [
        line.strip()
        for line in answer_part.split("\n")
        if line.strip()
    ]

    if not answer_lines:
        return []

    answer_part = answer_lines[0]

    # -----------------------------------------------------
    # Убираем варианты ответа из вопроса.
    # -----------------------------------------------------

    question_part = remove_choice_options(
        question_part,
        answer_part
    )

    question_part = clean_question(
        question_part
    )

    if not question_part:
        return []

    return [
        (
            None,
            question_part,
            answer_part
        )
    ]


# =========================================================
# ГЛАВНЫЙ ПАРСЕР
# =========================================================

def parse_questions(text):

    text = clean_input_text(text)

    if not text:
        return []

    telegram_parts = (
        split_by_telegram_messages(text)
    )

    all_questions = []

    # -----------------------------------------------------
    # Если это Telegram export
    # -----------------------------------------------------

    if len(telegram_parts) > 1:

        for part in telegram_parts:

            parsed = parse_single_message(
                part
            )

            if parsed:
                all_questions.extend(parsed)

        if all_questions:
            return normalize_questions(
                all_questions
            )

    # -----------------------------------------------------
    # Обычный текст
    # -----------------------------------------------------

    parsed = parse_single_message(text)

    if parsed:

        return normalize_questions(
            parsed
        )

    return []


# =========================================================
# НОМЕРА
# =========================================================

def normalize_questions(questions):

    result = []

    used_numbers = set()

    next_number = 1

    for number, question, answer in questions:

        if number is None:

            while next_number in used_numbers:
                next_number += 1

            number = next_number

        else:

            try:
                number = int(number)

            except Exception:

                while next_number in used_numbers:
                    next_number += 1

                number = next_number

        if number in used_numbers:

            while next_number in used_numbers:
                next_number += 1

            number = next_number

        used_numbers.add(number)

        next_number = max(
            next_number,
            number + 1
        )

        result.append(
            (
                number,
                question,
                answer
            )
        )

    return result


# =========================================================
# ФОРМАТИРОВАНИЕ
# =========================================================

def make_quote(text):

    lines = text.split("\n")

    result = []

    for line in lines:

        line = line.strip()

        if line:

            result.append(
                f"<blockquote>"
                f"{html.escape(line)}"
                f"</blockquote>"
            )

    return "\n".join(result)


def make_mobile(questions):

    if not questions:

        return (
            "❌ Не удалось найти "
            "вопросы и ответы."
        )

    parts = [
        "🤖 <b>РЕЗУЛЬТАТЫ</b>"
    ]

    for index, (
        number,
        question,
        answer
    ) in enumerate(
        questions,
        start=1
    ):

        safe_question = html.escape(
            question
        )

        quote = make_quote(answer)

        block = (
            f"🧩 <b>ЗАДАНИЕ {index}</b>\n\n"
            f"❓ <b>ВОПРОС</b>\n"
            f"{safe_question}\n\n"
            f"💬 <b>ОТВЕТ</b>\n"
            f"{quote}"
        )

        parts.append(block)

    return (
        "\n\n━━━━━━━━━━━━━━━━\n\n"
        .join(parts)
    )


def make_list(questions):

    if not questions:

        return (
            "❌ Не удалось найти "
            "вопросы и ответы."
        )

    parts = []

    for index, (
        number,
        question,
        answer
    ) in enumerate(
        questions,
        start=1
    ):

        safe_question = html.escape(
            question
        )

        clean_answer_text = re.sub(
            r"\s+",
            " ",
            answer.replace("\n", " ")
        ).strip()

        safe_answer = html.escape(
            clean_answer_text
        )

        parts.append(
            f"🧩 <b>{index}.</b> "
            f"<b>{safe_question}</b>\n"
            f"💬 {safe_answer}"
        )

    return "\n\n".join(parts)


def make_compact(questions):

    if not questions:

        return (
            "❌ Не удалось найти "
            "вопросы и ответы."
        )

    parts = []

    for index, (
        number,
        question,
        answer
    ) in enumerate(
        questions,
        start=1
    ):

        formatted = []

        for line in answer.split("\n"):

            line = line.strip()

            if line:
                formatted.append(
                    html.escape(line)
                )

        answer_text = "\n".join(
            formatted
        )

        parts.append(
            f"🧩 <b>{index}.</b>\n"
            f"{make_quote(answer_text)}"
        )

    return "\n\n".join(parts)


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    user_id = update.effective_user.id

    create_user_if_needed(user_id)

    if is_authorized(user_id):

        await update.message.reply_text(

            "🤖 <b>Админский бот!</b>\n\n"
            "✅ <b>Доступ разрешён!</b>\n\n"
            "📥 Отправляй задания или "
            "ссылку Skysmart.\n\n"
            "Для обычных сообщений после "
            "окончания нажми:\n"
            "⏹ <b>Завершить ввод</b>",

            parse_mode="HTML"

        )

        return

    await update.message.reply_text(

        "🤖 <b>Админский бот!</b>\n\n"
        "🔐 <b>Требуется пароль</b>\n\n"
        "Для использования бота введи "
        "выданный пароль.\n\n"
        "Формат:\n"
        "<code>XXXX-XXXX</code>",

        parse_mode="HTML"

    )


# =========================================================
# /ID
# =========================================================

async def get_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    await update.message.reply_text(

        "🆔 <b>Твой Telegram ID</b>\n\n"
        f"<code>{user_id}</code>",

        parse_mode="HTML"

    )


# =========================================================
# /KEYS
# =========================================================

async def keys_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not is_owner(user_id):

        await update.message.reply_text(
            "⛔ Эта команда доступна только владельцу бота."
        )

        return

    result = make_password_list()

    await update.message.reply_text(
        result,
        parse_mode="HTML"
    )


# =========================================================
# SKYSMART
# =========================================================

async def handle_skysmart_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    room_name: str
):

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    loading_message = await update.message.reply_text(

        "🎓 <b>Skysmart</b>\n\n"
        "🔎 Получаю задание...\n\n"
        f"🏷️ Комната: "
        f"<code>{html.escape(room_name)}</code>\n\n"
        "⏳ Пожалуйста, подожди.",

        parse_mode="HTML"

    )

    try:

        data = await asyncio.to_thread(
            get_skysmart_answers,
            room_name
        )

        tasks = get_skysmart_tasks(data)

        if not tasks:

            await loading_message.edit_text(

                "❌ <b>Ответы не найдены.</b>\n\n"
                "Возможно:\n"
                "• ссылка недействительна;\n"
                "• задание недоступно;\n"
                "• сервер не нашёл комнату.",

                parse_mode="HTML"

            )

            return

        user_last_skysmart_data[user_id] = data
        user_last_skysmart_room[user_id] = room_name

        result = make_skysmart_mobile(data)

        messages = split_long_text(result)

        if len(messages) == 1:

            await loading_message.edit_text(

                messages[0],
                parse_mode="HTML",
                reply_markup=get_result_keyboard()

            )

            return

        try:
            await loading_message.delete()
        except Exception:
            pass

        await send_result(
            context.bot,
            chat_id,
            result
        )

    except requests.exceptions.Timeout:

        await loading_message.edit_text(

            "⏳ <b>Сервер слишком долго отвечает.</b>\n\n"
            "Попробуй ещё раз позже.",

            parse_mode="HTML"

        )

    except requests.exceptions.ConnectionError:

        await loading_message.edit_text(

            "❌ <b>Не удалось подключиться "
            "к серверу.</b>\n\n"
            "Попробуй ещё раз позже.",

            parse_mode="HTML"

        )

    except requests.exceptions.HTTPError as error:

        print(
            "Skysmart HTTP Error:",
            error
        )

        await loading_message.edit_text(

            "❌ <b>Сервер вернул ошибку.</b>\n\n"
            "Возможно, комната не найдена.",

            parse_mode="HTML"

        )

    except ValueError:

        await loading_message.edit_text(

            "❌ <b>Некорректный ответ сервера.</b>",

            parse_mode="HTML"

        )

    except Exception as error:

        print(
            "Ошибка Skysmart:",
            error
        )

        await loading_message.edit_text(

            "❌ <b>Произошла ошибка.</b>\n\n"
            "Попробуй ещё раз.",

            parse_mode="HTML"

        )


# =========================================================
# ПОЛУЧЕНИЕ ТЕКСТА
# =========================================================

async def echo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.text:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    text = update.message.text.strip()

    # -----------------------------------------------------
    # АВТОРИЗАЦИЯ
    # -----------------------------------------------------

    if not is_authorized(user_id):

        create_user_if_needed(user_id)

        success, status = use_password(
            text,
            user_id
        )

        if success:

            await update.message.reply_text(

                "🤖 <b>Админский бот!</b>\n\n"
                "✅ <b>Пароль принят!</b>\n\n"
                "Теперь можешь отправлять "
                "задания или ссылки Skysmart.",

                parse_mode="HTML"

            )

            return

        if status == "used":

            await update.message.reply_text(
                "❌ Этот пароль уже использован.\n\n"
                "Получи новый пароль."
            )

            return

        if status == "error":

            await update.message.reply_text(
                "⚠️ Ошибка при проверке пароля.\n\n"
                "Попробуй ещё раз."
            )

            return

        await update.message.reply_text(

            "❌ <b>Неверный пароль.</b>\n\n"
            "Введите пароль в формате:\n\n"
            "<code>XXXX-XXXX</code>",

            parse_mode="HTML"

        )

        return

    # -----------------------------------------------------
    # SKYSMART
    # -----------------------------------------------------

    room_name = extract_skysmart_room(text)

    if room_name:

        await handle_skysmart_link(
            update,
            context,
            room_name
        )

        return

    # -----------------------------------------------------
    # ОБЫЧНЫЙ БУФЕР
    # -----------------------------------------------------

    if user_id not in user_buffers:
        user_buffers[user_id] = []

    if len(user_buffers[user_id]) >= MAX_INPUT_MESSAGES:

        await update.message.reply_text(

            f"⚠️ Достигнут лимит: "
            f"<b>{MAX_INPUT_MESSAGES}</b> сообщений.\n\n"
            "Нажми «⏹ Завершить ввод».",

            parse_mode="HTML"

        )

        return

    current_length = sum(
        len(message)
        for message in user_buffers[user_id]
    )

    if current_length + len(text) > MAX_INPUT_LENGTH:

        await update.message.reply_text(

            "⚠️ Достигнут лимит "
            "размера текста.\n\n"
            "Нажми «⏹ Завершить ввод».",

            parse_mode="HTML"

        )

        return

    user_buffers[user_id].append(text)

    # -----------------------------------------------------
    # УДАЛЯЕМ СТАРУЮ КНОПКУ
    # -----------------------------------------------------

    old_message_id = (
        user_last_control_message.get(user_id)
    )

    if old_message_id:

        try:

            await context.bot.edit_message_reply_markup(

                chat_id=chat_id,
                message_id=old_message_id,
                reply_markup=None

            )

        except Exception as error:

            print(
                "Ошибка удаления старой кнопки:",
                error
            )

    # -----------------------------------------------------
    # НОВАЯ КНОПКА
    # -----------------------------------------------------

    count = len(
        user_buffers[user_id]
    )

    control_message = await context.bot.send_message(

        chat_id=chat_id,

        text=(

            "📥 <b>Сообщение добавлено!</b>\n\n"
            f"📦 Получено сообщений: <b>{count}</b>\n\n"
            "Можешь отправить ещё сообщения "
            "или завершить ввод."

        ),

        parse_mode="HTML",

        reply_markup=get_finish_keyboard()

    )

    user_last_control_message[user_id] = (
        control_message.message_id
    )


# =========================================================
# ДЕЛЕНИЕ ДЛИННОГО РЕЗУЛЬТАТА
# =========================================================

def split_long_text(text):

    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    separator = "\n\n━━━━━━━━━━━━━━━━\n\n"

    blocks = text.split(separator)

    result = []
    current = ""

    for block in blocks:

        if len(block) > MAX_MESSAGE_LENGTH:

            if current:
                result.append(current)
                current = ""

            remaining = block

            while len(remaining) > MAX_MESSAGE_LENGTH:

                cut = remaining.rfind(
                    "\n",
                    0,
                    MAX_MESSAGE_LENGTH
                )

                if cut < 100:
                    cut = MAX_MESSAGE_LENGTH

                result.append(
                    remaining[:cut]
                )

                remaining = remaining[cut:].lstrip()

            if remaining:
                current = remaining

            continue

        if not current:

            current = block
            continue

        if (
            len(current)
            + len(separator)
            + len(block)
            <= MAX_MESSAGE_LENGTH
        ):

            current += separator + block

        else:

            result.append(current)
            current = block

    if current:
        result.append(current)

    return result


# =========================================================
# ОТПРАВКА РЕЗУЛЬТАТА
# =========================================================

async def send_result(
    bot,
    chat_id,
    result
):

    messages = split_long_text(result)

    for index, message_text in enumerate(messages):

        keyboard = None

        if index == len(messages) - 1:
            keyboard = get_result_keyboard()

        await bot.send_message(

            chat_id=chat_id,

            text=message_text,

            parse_mode="HTML",

            reply_markup=keyboard

        )


# =========================================================
# ЗАВЕРШЕНИЕ ВВОДА
# =========================================================

async def finish_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    user_id = query.from_user.id

    if not is_authorized(user_id):

        await query.answer(
            "⛔ Нет доступа.",
            show_alert=True
        )

        return

    await query.answer(
        "🔍 Обрабатываю..."
    )

    chat_id = query.message.chat_id

    messages = user_buffers.get(
        user_id,
        []
    )

    if not messages:

        await query.message.edit_text(
            "⚠️ Нет накопленных сообщений."
        )

        return

    combined_text = "\n\n".join(messages)

    questions = parse_questions(
        combined_text
    )

    if not questions:

        await query.message.edit_text(

            "❌ <b>Не удалось распознать задания.</b>\n\n"
            "Бот не смог найти пары:\n"
            "❓ Вопрос\n"
            "💬 Ответ\n\n"
            "Проверь, что в сообщениях есть "
            "слово «ОТВЕТ».",

            parse_mode="HTML"

        )

        user_buffers[user_id] = []

        return

    user_last_texts[user_id] = combined_text
    user_last_questions[user_id] = questions

    user_buffers[user_id] = []

    user_last_control_message.pop(
        user_id,
        None
    )

    result = make_mobile(
        questions
    )

    if len(result) <= MAX_MESSAGE_LENGTH:

        await query.message.edit_text(

            result,

            parse_mode="HTML",

            reply_markup=get_result_keyboard()

        )

        return

    try:
        await query.message.delete()
    except Exception:
        pass

    await send_result(
        context.bot,
        chat_id,
        result
    )


# =========================================================
# КНОПКИ
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    if query.data == "finish_input":

        await finish_input(
            update,
            context
        )

        return

    await query.answer()

    user_id = query.from_user.id

    if not is_authorized(user_id):

        await query.message.reply_text(
            "⛔ У тебя нет доступа."
        )

        return

    action = query.data

    # -----------------------------------------------------
    # SKYSMART
    # -----------------------------------------------------

    skysmart_data = (
        user_last_skysmart_data.get(user_id)
    )

    if skysmart_data:

        if action == "mobile":

            result = make_skysmart_mobile(
                skysmart_data
            )

        elif action == "list":

            result = make_skysmart_list(
                skysmart_data
            )

        elif action == "compact":

            result = make_skysmart_compact(
                skysmart_data
            )

        elif action == "repeat":

            result = make_skysmart_mobile(
                skysmart_data
            )

        else:

            return

        if len(result) <= MAX_MESSAGE_LENGTH:

            try:

                await query.message.edit_text(

                    result,

                    parse_mode="HTML",

                    reply_markup=get_result_keyboard()

                )

            except Exception as error:

                print(
                    "Ошибка изменения Skysmart сообщения:",
                    error
                )

            return

        try:

            await query.message.edit_reply_markup(
                reply_markup=None
            )

        except Exception:
            pass

        await send_result(
            context.bot,
            query.message.chat_id,
            result
        )

        return

    # -----------------------------------------------------
    # ОБЫЧНЫЕ ЗАДАНИЯ
    # -----------------------------------------------------

    questions = user_last_questions.get(
        user_id
    )

    if not questions:

        text = user_last_texts.get(
            user_id
        )

        if text:

            questions = parse_questions(
                text
            )

    if not questions:

        await query.message.reply_text(

            "⚠️ Исходные данные не найдены.\n\n"
            "Пришли задания ещё раз."

        )

        return

    if action == "mobile":

        result = make_mobile(
            questions
        )

    elif action == "list":

        result = make_list(
            questions
        )

    elif action == "compact":

        result = make_compact(
            questions
        )

    elif action == "repeat":

        result = make_mobile(
            questions
        )

    else:

        return

    if len(result) <= MAX_MESSAGE_LENGTH:

        try:

            await query.message.edit_text(

                result,

                parse_mode="HTML",

                reply_markup=get_result_keyboard()

            )

        except Exception as error:

            print(
                "Ошибка изменения сообщения:",
                error
            )

        return

    try:

        await query.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    await send_result(
        context.bot,
        query.message.chat_id,
        result
    )


# =========================================================
# TELEGRAM APPLICATION
# =========================================================

telegram_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


telegram_app.add_handler(
    CommandHandler(
        "start",
        start
    )
)

telegram_app.add_handler(
    CommandHandler(
        "id",
        get_id
    )
)

telegram_app.add_handler(
    CommandHandler(
        "keys",
        keys_command
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        echo
    )
)

telegram_app.add_handler(
    CallbackQueryHandler(
        button_handler
    )
)


# =========================================================
# WEBHOOK
# =========================================================

async def telegram_webhook(
    request: Request
) -> Response:

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot
    )

    await telegram_app.update_queue.put(
        update
    )

    return Response("OK")


# =========================================================
# HEALTH
# =========================================================

async def health(
    request: Request
) -> PlainTextResponse:

    return PlainTextResponse(
        "Bot is running!"
    )


# =========================================================
# STARLETTE
# =========================================================

web_app = Starlette(

    routes=[

        Route(
            "/",
            health
        ),

        Route(
            "/health",
            health
        ),

        Route(
            "/telegram",
            telegram_webhook,
            methods=["POST"]
        ),

    ]

)


# =========================================================
# MAIN
# =========================================================

async def main():

    if not BOT_TOKEN:
        raise ValueError(
            "Не задана BOT_TOKEN"
        )

    if not RENDER_URL:
        raise ValueError(
            "Не задана RENDER_URL"
        )

    if not SUPABASE_URL:
        raise ValueError(
            "Не задана SUPABASE_URL"
        )

    if not SUPABASE_SERVICE_KEY:
        raise ValueError(
            "Не задана SUPABASE_SERVICE_KEY"
        )

    if not OWNER_ID:
        raise ValueError(
            "Не задана OWNER_ID"
        )

    # -----------------------------------------------------
    # ПАРОЛИ
    # -----------------------------------------------------

    initialize_passwords()

    # -----------------------------------------------------
    # TELEGRAM
    # -----------------------------------------------------

    await telegram_app.initialize()

    await telegram_app.start()

    # -----------------------------------------------------
    # WEBHOOK
    # -----------------------------------------------------

    await telegram_app.bot.set_webhook(

        url=f"{RENDER_URL}/telegram",

        allowed_updates=Update.ALL_TYPES

    )

    # -----------------------------------------------------
    # UVICORN
    # -----------------------------------------------------

    config = uvicorn.Config(

        web_app,

        host="0.0.0.0",

        port=PORT

    )

    server = uvicorn.Server(config)

    print(
        f"Бот запущен на порту {PORT}"
    )

    print(
        f"Webhook: {RENDER_URL}/telegram"
    )

    try:

        await server.serve()

    finally:

        await telegram_app.stop()

        await telegram_app.shutdown()


# =========================================================
# ЗАПУСК
# =========================================================

if __name__ == "__main__":

    asyncio.run(main())
