import os
import asyncio
import re
import html
import secrets
import hashlib

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


# =========================================================
# SUPABASE
# =========================================================

supabase: Client | None = None


def get_supabase():

    global supabase

    if supabase is None:

        if not SUPABASE_URL:
            raise ValueError("Не задана переменная SUPABASE_URL")

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


# =========================================================
# ГЕНЕРАЦИЯ ПАРОЛЕЙ
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


# =========================================================
# СОЗДАНИЕ ПАРОЛЕЙ
# =========================================================

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

            db.table(
                "bot_passwords"
            ).insert(
                {
                    "password_hash":
                        hash_password(password),

                    "password_text":
                        password,

                    "used":
                        False,

                    "used_by":
                        None,

                    "used_at":
                        None,
                }
            ).execute()

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
# ПРОВЕРКА АВТОРИЗАЦИИ
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


# =========================================================
# СОЗДАНИЕ ПОЛЬЗОВАТЕЛЯ
# =========================================================

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

        db.table(
            "bot_users"
        ).insert(
            {
                "telegram_id":
                    telegram_id,

                "authorized":
                    False
            }
        ).execute()

    except Exception as error:

        print(
            "Ошибка создания пользователя:",
            error
        )


# =========================================================
# ИСПОЛЬЗОВАНИЕ ПАРОЛЯ
# =========================================================

def use_password(password, telegram_id):

    db = get_supabase()

    password = password.strip().upper()

    password_hash = hash_password(password)

    try:

        result = (
            db.table("bot_passwords")
            .select(
                "id,used,password_text"
            )
            .eq(
                "password_hash",
                password_hash
            )
            .limit(1)
            .execute()
        )

        if not result.data:
            return False, "wrong"

        row = result.data[0]

        if row.get("used"):
            return False, "used"

        db.table(
            "bot_passwords"
        ).update(
            {
                "used":
                    True,

                "used_by":
                    telegram_id,

                "used_at":
                    "now()",
            }
        ).eq(
            "id",
            row["id"]
        ).execute()

        db.table(
            "bot_users"
        ).update(
            {
                "authorized":
                    True
            }
        ).eq(
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
            .select(
                "password_text,used,used_by"
            )
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


# =========================================================
# СПИСОК ПАРОЛЕЙ
# =========================================================

def make_password_list():

    rows, total, used, free = (
        get_password_stats()
    )

    result = (
        "🔐 <b>ПАРОЛИ ДОСТУПА</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"Использовано: <b>{used}</b>\n"
        f"Свободно: <b>{free}</b>\n\n"
    )

    for index, row in enumerate(
        rows,
        start=1
    ):

        password = row.get(
            "password_text",
            "???"
        )

        if row.get("used"):

            used_by = row.get("used_by")

            result += (
                f"🔴 {index}. "
                f"<s>{password}</s>"
                " — использован"
            )

            if used_by:
                result += f" ({used_by})"

            result += "\n"

        else:

            result += (
                f"🟢 {index}. "
                f"<code>{password}</code>"
                " — свободен\n"
            )

    return result


# =========================================================
# ПРОВЕРКА ВЛАДЕЛЬЦА
# =========================================================

def is_owner(telegram_id):

    if not OWNER_ID:
        return False

    return str(telegram_id) == str(OWNER_ID)


# =========================================================
# КЛАВИАТУРА "ЗАВЕРШИТЬ ВВОД"
# =========================================================

def get_finish_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏹ Завершить ввод",
                    callback_data="finish_input"
                )
            ]
        ]
    )


# =========================================================
# КЛАВИАТУРА РЕЗУЛЬТАТА
# =========================================================

def get_result_keyboard():

    return InlineKeyboardMarkup(
        [
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
        ]
    )


# =========================================================
# ОЧИСТКА TELEGRAM ЭКСПОРТА
# =========================================================

def remove_telegram_export_header(text):

    if not text:
        return ""

    # Например:
    #
    # [01.09.2026 14:35] Kirill Rem:
    #
    # или:
    #
    # [28.08.2026 23:31] Kirill Rem:
    #
    pattern = re.compile(
        r"^\s*"
        r"\[\d{1,2}\.\d{1,2}\.\d{4}"
        r"\s+\d{1,2}:\d{2}\]"
        r"\s*"
        r"[^:\n]{0,150}"
        r":\s*",
        re.MULTILINE
    )

    return pattern.sub(
        "",
        text
    )


# =========================================================
# ОЧИСТКА ТЕКСТА
# =========================================================

def clean_input_text(text):

    if not text:
        return ""

    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    # Telegram export
    text = remove_telegram_export_header(
        text
    )

    # "Получил сообщение"
    text = re.sub(
        r"Получил\s+сообщение\s*:?",
        "",
        text,
        flags=re.IGNORECASE
    )

    # Markdown-ссылки
    text = re.sub(
        r"\[([^\]]+)\]\([^)]+\)",
        r"\1",
        text
    )

    # Обычные ссылки
    text = re.sub(
        r"https?://\S+",
        "",
        text
    )

    # Markdown
    text = text.replace(
        "**",
        ""
    )

    text = text.replace(
        "__",
        ""
    )

    text = text.replace(
        "`",
        ""
    )

    # Унификация вариантов ответа
    text = re.sub(
        r"\[\s*ОТВЕТ\s*\]",
        "ОТВЕТ:",
        text,
        flags=re.IGNORECASE
    )

    # Убираем слишком много пустых строк
    text = re.sub(
        r"[ \t]+\n",
        "\n",
        text
    )

    text = re.sub(
        r"\n[ \t]+",
        "\n",
        text
    )

    text = re.sub(
        r"\n{4,}",
        "\n\n",
        text
    )

    return text.strip()


# =========================================================
# ОЧИСТКА ОТВЕТА
# =========================================================

def clean_answer(answer):

    if not answer:
        return ""

    answer = answer.strip()

    answer = re.sub(
        r"^\s*(?:ОТВЕТ|Ответ)\s*:?\s*",
        "",
        answer,
        flags=re.IGNORECASE
    )

    lines = []

    for line in answer.split("\n"):

        line = line.rstrip()

        if line.strip():
            lines.append(line)

    answer = "\n".join(lines)

    return answer.strip()


# =========================================================
# ОЧИСТКА ВОПРОСА
# =========================================================

def clean_question(question):

    if not question:
        return ""

    question = question.strip()

    # Вопрос 1:
    # Вопрос 1.
    # Вопрос 1)
    question = re.sub(
        r"^\s*Вопрос\s+\d+\s*[:.)-]?\s*",
        "",
        question,
        flags=re.IGNORECASE
    )

    # Вопрос:
    question = re.sub(
        r"^\s*Вопрос\s*:\s*",
        "",
        question,
        flags=re.IGNORECASE
    )

    # Просто "1." или "1)"
    question = re.sub(
        r"^\s*\d+\s*[\.\)]\s*",
        "",
        question
    )

    lines = []

    for line in question.split("\n"):

        line = line.strip()

        if line:
            lines.append(line)

    return "\n".join(lines).strip()


# =========================================================
# РАЗДЕЛЕНИЕ TELEGRAM-СООБЩЕНИЙ
# =========================================================

def split_by_telegram_messages(text):

    pattern = re.compile(
        r"(?="
        r"\[\d{1,2}\.\d{1,2}\.\d{4}"
        r"\s+\d{1,2}:\d{2}\]"
        r")"
    )

    parts = pattern.split(text)

    result = []

    for part in parts:

        part = part.strip()

        if part:
            result.append(part)

    return result


# =========================================================
# ПОИСК ГРАНИЦЫ СЛЕДУЮЩЕГО ВОПРОСА
# =========================================================

QUESTION_HEADER_PATTERN = (
    r"(?:"
    r"Вопрос\s+\d+\s*[:.)-]"
    r"|"
    r"Вопрос\s*:"
    r"|"
    r"\d+\s*[\.)-]"
    r")"
)


# =========================================================
# ОСНОВНОЙ ПАРСЕР
# =========================================================

def parse_question_answer_format(text):

    questions = []

    if not text:
        return questions

    # -----------------------------------------------------
    # ВАРИАНТ 1
    #
    # Вопрос 5:
    # Текст вопроса
    #
    # Ответ:
    # Текст ответа
    # -----------------------------------------------------

    pattern = re.compile(
        r"(?:^|\n)"
        r"\s*"
        r"(?:Вопрос\s+(\d+)\s*[:.)-]"
        r"|Вопрос\s*:"
        r"|(\d+)\s*[\.)-])"
        r"\s*"
        r"(.*?)"
        r"\n\s*"
        r"(?:ОТВЕТ|Ответ)"
        r"\s*:\s*"
        r"(.*?)"
        r"(?="
        r"\n\s*"
        r"(?:" + QUESTION_HEADER_PATTERN + r")"
        r"\s*"
        r"|"
        r"\Z"
        r")",
        re.IGNORECASE | re.DOTALL
    )

    for match in pattern.finditer(text):

        number = (
            match.group(1)
            or
            match.group(2)
        )

        question = match.group(3)

        answer = match.group(4)

        question = clean_question(
            question
        )

        answer = clean_answer(
            answer
        )

        if question and answer:

            questions.append(
                (
                    number,
                    question,
                    answer
                )
            )

    if questions:
        return questions

    # -----------------------------------------------------
    # ВАРИАНТ 2
    #
    # Вопрос:
    # ...
    #
    # ОТВЕТ:
    # ...
    #
    # без номера
    # -----------------------------------------------------

    pattern_without_number = re.compile(
        r"(?:^|\n)"
        r"\s*Вопрос\s*:\s*"
        r"(.*?)"
        r"\n\s*"
        r"(?:ОТВЕТ|Ответ)"
        r"\s*:\s*"
        r"(.*?)"
        r"(?="
        r"\n\s*Вопрос\s*(?:\d+\s*)?:"
        r"|"
        r"\Z"
        r")",
        re.IGNORECASE | re.DOTALL
    )

    for match in pattern_without_number.finditer(text):

        question = clean_question(
            match.group(1)
        )

        answer = clean_answer(
            match.group(2)
        )

        if question and answer:

            questions.append(
                (
                    None,
                    question,
                    answer
                )
            )

    return questions


# =========================================================
# ПАРСЕР ФОРМАТА "ВОПРОС ... ОТВЕТ"
# =========================================================

def parse_flexible_question_answer(text):

    questions = []

    if not text:
        return questions

    # Ищем все маркеры ОТВЕТ.
    answer_matches = list(
        re.finditer(
            r"(?:^|\n)\s*(?:\[)?ОТВЕТ(?:\])?\s*:?",
            text,
            flags=re.IGNORECASE
        )
    )

    if not answer_matches:
        return []

    for index, answer_match in enumerate(answer_matches):

        # Всё после предыдущего ответа
        # и до текущего ответа является вопросом.

        if index == 0:

            question_start = 0

        else:

            question_start = (
                answer_matches[index - 1].end()
            )

        question_part = text[
            question_start:
            answer_match.start()
        ]

        # Ответ заканчивается перед следующим
        # явным "Вопрос ..."

        answer_start = answer_match.end()

        next_question = re.search(
            r"\n\s*"
            r"(?:Вопрос\s+\d+\s*[:.)-]"
            r"|Вопрос\s*:"
            r"|\d+\s*[\.)-])",
            text[answer_start:],
            flags=re.IGNORECASE
        )

        if next_question:

            answer_end = (
                answer_start
                + next_question.start()
            )

        else:

            answer_end = len(text)

        answer_part = text[
            answer_start:
            answer_end
        ]

        question_part = clean_question(
            question_part
        )

        answer_part = clean_answer(
            answer_part
        )

        if not question_part or not answer_part:
            continue

        # Пытаемся достать номер из вопроса
        number_match = re.search(
            r"^\s*Вопрос\s+(\d+)",
            question_part,
            flags=re.IGNORECASE
        )

        if number_match:

            number = number_match.group(1)

        else:

            number_match = re.search(
                r"^\s*(\d+)\s*[\.)-]",
                question_part
            )

            if number_match:
                number = number_match.group(1)
            else:
                number = None

        questions.append(
            (
                number,
                question_part,
                answer_part
            )
        )

    return questions


# =========================================================
# НОРМАЛИЗАЦИЯ
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
# УДАЛЕНИЕ ДУБЛИКАТОВ
# =========================================================

def remove_duplicate_questions(questions):

    result = []

    seen = set()

    for number, question, answer in questions:

        key = (
            re.sub(
                r"\s+",
                " ",
                question.lower()
            ).strip(),

            re.sub(
                r"\s+",
                " ",
                answer.lower()
            ).strip()
        )

        if key in seen:
            continue

        seen.add(key)

        result.append(
            (
                number,
                question,
                answer
            )
        )

    return result


# =========================================================
# ГЛАВНЫЙ ПАРСЕР
# =========================================================

def parse_questions(text):

    text = clean_input_text(text)

    if not text:
        return []

    all_questions = []

    # -----------------------------------------------------
    # 1. Сначала обрабатываем Telegram export
    # -----------------------------------------------------

    telegram_parts = (
        split_by_telegram_messages(text)
    )

    if len(telegram_parts) > 1:

        for part in telegram_parts:

            cleaned_part = clean_input_text(
                part
            )

            if not cleaned_part:
                continue

            parsed = parse_question_answer_format(
                cleaned_part
            )

            if not parsed:

                parsed = parse_flexible_question_answer(
                    cleaned_part
                )

            if parsed:
                all_questions.extend(parsed)

        if all_questions:

            all_questions = (
                remove_duplicate_questions(
                    all_questions
                )
            )

            return normalize_questions(
                all_questions
            )

    # -----------------------------------------------------
    # 2. Обычный парсер
    # -----------------------------------------------------

    parsed = parse_question_answer_format(
        text
    )

    if parsed:

        parsed = remove_duplicate_questions(
            parsed
        )

        return normalize_questions(
            parsed
        )

    # -----------------------------------------------------
    # 3. Гибкий парсер
    # -----------------------------------------------------

    parsed = parse_flexible_question_answer(
        text
    )

    if parsed:

        parsed = remove_duplicate_questions(
            parsed
        )

        return normalize_questions(
            parsed
        )

    # -----------------------------------------------------
    # 4. Последняя попытка:
    #    разделяем по ОТВЕТ
    # -----------------------------------------------------

    parts = re.split(
        r"(?i)"
        r"(?:\n|\A)"
        r"\s*(?:\[)?ОТВЕТ(?:\])?"
        r"\s*:\s*",
        text
    )

    if len(parts) >= 2:

        questions = []

        for i in range(
            len(parts) - 1
        ):

            question = parts[i].strip()

            answer = parts[i + 1].strip()

            # В первой части может быть несколько
            # технических строк.
            question = clean_question(
                question
            )

            answer = clean_answer(
                answer
            )

            if question and answer:

                questions.append(
                    (
                        None,
                        question,
                        answer
                    )
                )

        if questions:

            questions = remove_duplicate_questions(
                questions
            )

            return normalize_questions(
                questions
            )

    return []


# =========================================================
# ЦИТАТА
# =========================================================

def make_quote(text):

    lines = text.split("\n")

    result = []

    for line in lines:

        line = line.strip()

        if line:

            result.append(
                "<blockquote>"
                + html.escape(line)
                + "</blockquote>"
            )

    return "\n".join(result)


# =========================================================
# МОБИЛЬНЫЙ ФОРМАТ
# =========================================================

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

        quote = make_quote(
            answer
        )

        block = (
            f"🧩 <b>ЗАДАНИЕ {index}</b>\n\n"
            f"❓ <b>ВОПРОС</b>\n"
            f"{safe_question}\n\n"
            f"💬 <b>ОТВЕТ</b>\n"
            f"{quote}"
        )

        parts.append(block)

    return "\n\n━━━━━━━━━━━━━━━━\n\n".join(
        parts
    )


# =========================================================
# СПИСОК
# =========================================================

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
            answer.replace(
                "\n",
                " "
            )
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


# =========================================================
# ТОЛЬКО ОТВЕТЫ
# =========================================================

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
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    user_id = update.effective_user.id

    create_user_if_needed(
        user_id
    )

    if is_authorized(user_id):

        await update.message.reply_text(
            "🤖 <b>Админский бот!</b>\n\n"
            "✅ <b>Доступ разрешён!</b>\n\n"
            "📥 Отправляй одно или несколько "
            "сообщений с заданиями.\n\n"
            "После окончания нажми:\n"
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
# ID
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
# KEYS
# =========================================================

async def keys_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    if not is_owner(user_id):

        await update.message.reply_text(
            "⛔ Эта команда доступна "
            "только владельцу бота."
        )

        return

    result = make_password_list()

    await update.message.reply_text(
        result,
        parse_mode="HTML"
    )


# =========================================================
# ПОЛУЧЕНИЕ СООБЩЕНИЯ
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

        create_user_if_needed(
            user_id
        )

        success, status = use_password(
            text,
            user_id
        )

        if success:

            await update.message.reply_text(
                "🤖 <b>Админский бот!</b>\n\n"
                "✅ <b>Пароль принят!</b>\n\n"
                "Теперь можешь отправлять "
                "задания.",
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
    # СОЗДАНИЕ БУФЕРА
    # -----------------------------------------------------

    if user_id not in user_buffers:

        user_buffers[user_id] = []

    # -----------------------------------------------------
    # ЛИМИТ СООБЩЕНИЙ
    # -----------------------------------------------------

    if len(
        user_buffers[user_id]
    ) >= MAX_INPUT_MESSAGES:

        await update.message.reply_text(
            f"⚠️ Достигнут лимит: "
            f"<b>{MAX_INPUT_MESSAGES}</b> сообщений.\n\n"
            "Нажми «⏹ Завершить ввод».",
            parse_mode="HTML"
        )

        return

    # -----------------------------------------------------
    # ЛИМИТ СИМВОЛОВ
    # -----------------------------------------------------

    current_length = sum(
        len(message)
        for message in user_buffers[user_id]
    )

    if (
        current_length + len(text)
        > MAX_INPUT_LENGTH
    ):

        await update.message.reply_text(
            "⚠️ Достигнут лимит "
            "размера текста.\n\n"
            "Нажми «⏹ Завершить ввод».",
            parse_mode="HTML"
        )

        return

    # -----------------------------------------------------
    # ДОБАВЛЯЕМ СООБЩЕНИЕ
    # -----------------------------------------------------

    user_buffers[user_id].append(
        text
    )

    # -----------------------------------------------------
    # УДАЛЯЕМ СТАРУЮ КНОПКУ
    # -----------------------------------------------------

    old_message_id = (
        user_last_control_message.get(
            user_id
        )
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

    control_message = (
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "📥 <b>Сообщение добавлено!</b>\n\n"
                f"📦 Получено сообщений: "
                f"<b>{count}</b>\n\n"
                "Можешь отправить ещё сообщения "
                "или завершить ввод."
            ),
            parse_mode="HTML",
            reply_markup=get_finish_keyboard()
        )
    )

    user_last_control_message[user_id] = (
        control_message.message_id
    )


# =========================================================
# РАЗДЕЛЕНИЕ ДЛИННОГО РЕЗУЛЬТАТА
# =========================================================

def split_long_text(text):

    if len(text) <= MAX_MESSAGE_LENGTH:

        return [text]

    separator = (
        "\n\n━━━━━━━━━━━━━━━━\n\n"
    )

    blocks = text.split(
        separator
    )

    result = []

    current = ""

    for block in blocks:

        if not current:

            current = block

            continue

        if (
            len(current)
            + len(separator)
            + len(block)
            <= MAX_MESSAGE_LENGTH
        ):

            current += (
                separator
                + block
            )

        else:

            result.append(
                current
            )

            current = block

    if current:
        result.append(current)

    # На случай очень длинного отдельного блока
    final_result = []

    for block in result:

        if len(block) <= MAX_MESSAGE_LENGTH:

            final_result.append(block)

            continue

        # Telegram всё равно не примет такой кусок.
        # Режем аккуратно по строкам.

        current = ""

        for line in block.split("\n"):

            if (
                len(current)
                + len(line)
                + 1
                <= MAX_MESSAGE_LENGTH
            ):

                if current:
                    current += "\n"

                current += line

            else:

                if current:
                    final_result.append(
                        current
                    )

                current = line

        if current:
            final_result.append(
                current
            )

    return final_result


# =========================================================
# ОТПРАВКА РЕЗУЛЬТАТА
# =========================================================

async def send_result(
    bot,
    chat_id,
    result
):

    messages = split_long_text(
        result
    )

    for index, message_text in enumerate(
        messages
    ):

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

    # -----------------------------------------------------
    # ОБЪЕДИНЯЕМ СООБЩЕНИЯ
    # -----------------------------------------------------

    combined_text = "\n\n".join(
        messages
    )

    # -----------------------------------------------------
    # РАСПОЗНАЁМ
    # -----------------------------------------------------

    questions = parse_questions(
        combined_text
    )

    print(
        f"[PARSER] Пользователь {user_id}: "
        f"получено сообщений = {len(messages)}, "
        f"найдено заданий = {len(questions)}"
    )

    for index, (
        number,
        question,
        answer
    ) in enumerate(
        questions,
        start=1
    ):

        print(
            f"[PARSER] #{index} "
            f"номер={number} "
            f"вопрос={question[:80]!r} "
            f"ответ={answer[:80]!r}"
        )

    # -----------------------------------------------------
    # НИЧЕГО НЕ НАЙДЕНО
    # -----------------------------------------------------

    if not questions:

        await query.message.edit_text(
            "❌ <b>Не удалось распознать задания.</b>\n\n"
            "Бот не смог найти пары:\n\n"
            "❓ Вопрос\n"
            "💬 Ответ\n\n"
            "Попробуй проверить формат текста.",
            parse_mode="HTML"
        )

        user_buffers[user_id] = []

        user_last_control_message.pop(
            user_id,
            None
        )

        return

    # -----------------------------------------------------
    # СОХРАНЯЕМ
    # -----------------------------------------------------

    user_last_texts[user_id] = (
        combined_text
    )

    user_last_questions[user_id] = (
        questions
    )

    # -----------------------------------------------------
    # ОЧИЩАЕМ БУФЕР
    # -----------------------------------------------------

    user_buffers[user_id] = []

    user_last_control_message.pop(
        user_id,
        None
    )

    # -----------------------------------------------------
    # РЕЗУЛЬТАТ
    # -----------------------------------------------------

    result = make_mobile(
        questions
    )

    # -----------------------------------------------------
    # КОРОТКИЙ РЕЗУЛЬТАТ
    # -----------------------------------------------------

    if len(result) <= MAX_MESSAGE_LENGTH:

        await query.message.edit_text(
            result,
            parse_mode="HTML",
            reply_markup=get_result_keyboard()
        )

        return

    # -----------------------------------------------------
    # ДЛИННЫЙ РЕЗУЛЬТАТ
    # -----------------------------------------------------

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
# ОБРАБОТЧИК КНОПОК
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    # -----------------------------------------------------
    # ЗАВЕРШИТЬ ВВОД
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # ПОЛУЧАЕМ ПОСЛЕДНИЕ ЗАДАНИЯ
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

            if questions:

                user_last_questions[user_id] = (
                    questions
                )

    if not questions:

        await query.message.reply_text(
            "⚠️ Исходные данные не найдены.\n\n"
            "Пришли задания ещё раз."
        )

        return

    action = query.data

    # -----------------------------------------------------
    # ФОРМАТ
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # КОРОТКИЙ
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # ДЛИННЫЙ
    # -----------------------------------------------------

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

    server = uvicorn.Server(
        config
    )

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
