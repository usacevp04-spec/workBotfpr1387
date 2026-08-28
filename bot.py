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
# СОЗДАНИЕ 10 ПАРОЛЕЙ
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

            print(
                "Пароли уже существуют."
            )

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
        print("🔐 НОВЫЕ ПАРОЛИ ДОСТУПА")
        print("=" * 50)

        for password in generated:
            print(password)

        print("=" * 50)
        print("")

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
            .eq(
                "telegram_id",
                telegram_id
            )
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
            .eq(
                "telegram_id",
                telegram_id
            )
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
# ПРОВЕРКА ПАРОЛЯ
# =========================================================

def use_password(password, telegram_id):

    db = get_supabase()

    password = password.strip().upper()

    password_hash = hash_password(
        password
    )

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
                "used": True,
                "used_by": telegram_id,
                "used_at": "now()",
            }
        ).eq(
            "id",
            row["id"]
        ).execute()

        db.table(
            "bot_users"
        ).update(
            {
                "authorized": True
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

            used_by = row.get(
                "used_by"
            )

            result += (
                f"🔴 {index}. "
                f"<s>{password}</s>"
                f" — использован"
            )

            if used_by:
                result += (
                    f" ({used_by})"
                )

            result += "\n"

        else:

            result += (
                f"🟢 {index}. "
                f"<code>{password}</code>"
                f" — свободен\n"
            )

    return result


# =========================================================
# ВЛАДЕЛЕЦ
# =========================================================

def is_owner(telegram_id):

    if not OWNER_ID:
        return False

    return str(
        telegram_id
    ) == str(
        OWNER_ID
    )


# =========================================================
# ГЛАВНОЕ МЕНЮ
# =========================================================

def get_main_menu(user_id):

    buttons = [

        [
            InlineKeyboardButton(
                "📝 Начать ввод",
                callback_data="menu_start"
            )
        ],

        [
            InlineKeyboardButton(
                "📋 Последние ответы",
                callback_data="menu_last"
            )
        ],

        [
            InlineKeyboardButton(
                "🔄 Новый ввод",
                callback_data="menu_new"
            ),

            InlineKeyboardButton(
                "ℹ️ Помощь",
                callback_data="menu_help"
            )
        ],
    ]

    # Кнопки владельца
    if is_owner(user_id):

        buttons.append(
            [
                InlineKeyboardButton(
                    "🔐 Пароли",
                    callback_data="menu_keys"
                ),

                InlineKeyboardButton(
                    "📊 Статистика",
                    callback_data="menu_stats"
                )
            ]
        )

    return InlineKeyboardMarkup(
        buttons
    )


# =========================================================
# КНОПКА НАЗАД
# =========================================================

def get_back_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔙 Главное меню",
                    callback_data="menu_main"
                )
            ]
        ]
    )


# =========================================================
# КНОПКА ЗАВЕРШИТЬ ВВОД
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
# КНОПКИ РЕЗУЛЬТАТА
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

            [
                InlineKeyboardButton(
                    "🏠 Главное меню",
                    callback_data="menu_main"
                )
            ]

        ]
    )


# =========================================================
# РАЗБОР ВОПРОСОВ
# =========================================================

def parse_questions(text: str):

    text = text.strip()

    if not text:
        return []

    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    pattern = re.compile(
        r"Вопрос\s+(\d+)\s*:\s*"
        r"(.*?)"
        r"\bОтвет\s*:\s*"
        r"(.*?)"
        r"(?=\n\s*Вопрос\s+\d+\s*:|\Z)",
        re.IGNORECASE | re.DOTALL
    )

    questions = []

    for match in pattern.finditer(text):

        number = int(
            match.group(1)
        )

        question = match.group(2).strip()

        answer = match.group(3).strip()

        question = re.sub(
            r"\n\s*",
            " ",
            question
        )

        question = re.sub(
            r"\s+",
            " ",
            question
        )

        question = question.strip()

        answer = re.sub(
            r"Получил\s+сообщение\s*:?",
            "",
            answer,
            flags=re.IGNORECASE
        )

        answer = re.sub(
            r"^\s*Получил\s+сообщение\s*:?\s*",
            "",
            answer,
            flags=re.IGNORECASE
        )

        answer = re.sub(
            r"\s*Получил\s+сообщение\s*:?\s*$",
            "",
            answer,
            flags=re.IGNORECASE
        )

        answer = "\n".join(
            line.rstrip()
            for line in answer.split("\n")
        )

        answer = re.sub(
            r"\n\s*\n+",
            "\n",
            answer
        )

        answer = answer.strip()

        if question and answer:

            questions.append(
                (
                    number,
                    question,
                    answer
                )
            )

    return questions


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
        "📝 <b>ОТВЕТЫ</b>"
    ]

    for number, question, answer in questions:

        safe_question = html.escape(
            question
        )

        safe_answer = html.escape(
            answer
        )

        answer_lines = (
            safe_answer.split("\n")
        )

        formatted_answer = []

        for index, line in enumerate(
            answer_lines
        ):

            if index == 0:

                formatted_answer.append(
                    f"✅ {line}"
                )

            else:

                formatted_answer.append(
                    f"   {line}"
                )

        formatted_answer = "\n".join(
            formatted_answer
        )

        block = (
            f"❓ <b>{number}. "
            f"{safe_question}</b>\n\n"
            f"{formatted_answer}"
        )

        parts.append(
            block
        )

    return "\n\n\n".join(
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

    for number, question, answer in questions:

        safe_question = html.escape(
            question
        )

        clean_answer = answer.replace(
            "\n",
            " "
        )

        clean_answer = re.sub(
            r"\s+",
            " ",
            clean_answer
        )

        clean_answer = clean_answer.strip()

        safe_answer = html.escape(
            clean_answer
        )

        parts.append(
            f"<b>{number}. "
            f"{safe_question}</b>\n"
            f"→ {safe_answer}"
        )

    return "\n\n".join(
        parts
    )


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

    for number, question, answer in questions:

        clean_answer = answer.replace(
            "\n",
            " "
        )

        clean_answer = re.sub(
            r"\s+",
            " ",
            clean_answer
        )

        clean_answer = clean_answer.strip()

        parts.append(
            f"<b>{number}.</b> "
            f"{html.escape(clean_answer)}"
        )

    return "\n".join(
        parts
    )


# =========================================================
# ГЛАВНОЕ МЕНЮ
# =========================================================

async def show_main_menu(
    message,
    user_id,
    edit=False
):

    text = (
        "💋<b>Админский бот 1387</b>\n\n"
        "Добро пожаловать!\n\n"
        "Выбери нужное действие:"
    )

    keyboard = get_main_menu(
        user_id
    )

    if edit:

        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    else:

        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )


# =========================================================
# ПОМОЩЬ
# =========================================================

async def show_help(
    message,
    edit=False
):

    text = (
        "ℹ️ <b>Как пользоваться ботом</b>\n\n"

        "1️⃣ Нажми <b>📝 Начать ввод</b>.\n\n"

        "2️⃣ Отправь одно или несколько "
        "сообщений с заданиями.\n\n"

        "3️⃣ Когда закончишь отправлять "
        "сообщения, нажми "
        "<b>⏹ Завершить ввод</b>.\n\n"

        "4️⃣ Бот соберёт все вопросы и "
        "ответы в одно удобное сообщение.\n\n"

        "После обработки можно выбрать "
        "нужный формат отображения."
    )

    keyboard = get_back_keyboard()

    if edit:

        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    else:

        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )


# =========================================================
# НАЧАТЬ НОВЫЙ ВВОД
# =========================================================

async def start_input(
    message,
    user_id,
    context
):

    # Полностью очищаем старый ввод
    user_buffers[user_id] = []

    old_message_id = (
        user_last_control_message.get(
            user_id
        )
    )

    if old_message_id:

        try:

            await context.bot.edit_message_reply_markup(
                chat_id=message.chat_id,
                message_id=old_message_id,
                reply_markup=None
            )

        except Exception:
            pass

    user_last_control_message.pop(
        user_id,
        None
    )

    text = (
        "📝 <b>Режим ввода включён</b>\n\n"
        "Теперь отправляй мне одно или "
        "несколько сообщений с вопросами "
        "и ответами.\n\n"
        "Когда закончишь — нажми "
        "<b>⏹ Завершить ввод</b>."
    )

    await message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_finish_keyboard()
    )

    user_last_control_message[user_id] = (
        message.message_id
    )


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    user_id = (
        update.effective_user.id
    )

    create_user_if_needed(
        user_id
    )

    if is_authorized(user_id):

        await show_main_menu(
            update.message,
            user_id
        )

        return

    await update.message.reply_text(
        "🔐 <b>Требуется пароль</b>\n\n"
        "Для использования бота введи "
        "выданный тебе пароль.\n\n"
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

    user_id = (
        update.effective_user.id
    )

    await update.message.reply_text(
        f"🆔 Твой Telegram ID:\n\n"
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

    user_id = (
        update.effective_user.id
    )

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
# СТАТИСТИКА
# =========================================================

async def show_stats(
    message,
    edit=False
):

    rows, total, used, free = (
        get_password_stats()
    )

    text = (
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"🔐 Всего паролей: <b>{total}</b>\n"
        f"🔴 Использовано: <b>{used}</b>\n"
        f"🟢 Свободно: <b>{free}</b>\n"
    )

    keyboard = get_back_keyboard()

    if edit:

        await message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    else:

        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )


# =========================================================
# ТЕКСТОВЫЕ СООБЩЕНИЯ
# =========================================================

async def echo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.text:
        return

    user_id = (
        update.effective_user.id
    )

    chat_id = (
        update.effective_chat.id
    )

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
                "✅ <b>Пароль принят!</b>\n\n"
                "Доступ к боту открыт.",
                parse_mode="HTML"
            )

            await show_main_menu(
                update.message,
                user_id
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
                "⚠️ Ошибка при проверке "
                "пароля."
            )

            return

        await update.message.reply_text(
            "❌ Неверный пароль.\n\n"
            "Введите действующий пароль:\n\n"
            "<code>XXXX-XXXX</code>",
            parse_mode="HTML"
        )

        return

    # -----------------------------------------------------
    # ДОБАВЛЯЕМ СООБЩЕНИЕ
    # -----------------------------------------------------

    if user_id not in user_buffers:

        user_buffers[user_id] = []

    user_buffers[user_id].append(
        text
    )

    # -----------------------------------------------------
    # Убираем кнопку со старого сообщения
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

        except Exception:
            pass

    # -----------------------------------------------------
    # Новое последнее сообщение
    # -----------------------------------------------------

    control_message = (
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Ответы собираются...",
            reply_markup=get_finish_keyboard()
        )
    )

    user_last_control_message[user_id] = (
        control_message.message_id
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

    user_id = (
        query.from_user.id
    )

    if not is_authorized(user_id):

        await query.answer(
            "⛔ Нет доступа.",
            show_alert=True
        )

        return

    await query.answer(
        "Обрабатываю..."
    )

    chat_id = query.message.chat_id

    messages = user_buffers.get(
        user_id,
        []
    )

    if not messages:

        await query.message.edit_text(
            "⚠️ Нет накопленных сообщений.",
            reply_markup=get_back_keyboard()
        )

        return

    combined_text = "\n\n".join(
        messages
    )

    questions = parse_questions(
        combined_text
    )

    if not questions:

        await query.message.edit_text(
            "❌ Я не смог найти пары "
            "«Вопрос → Ответ».\n\n"
            "Проверь формат входных сообщений.",
            reply_markup=get_back_keyboard()
        )

        user_buffers[user_id] = []

        return

    user_last_texts[user_id] = (
        combined_text
    )

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

    blocks = result.split(
        "\n\n\n"
    )

    current = ""

    for block in blocks:

        if not current:

            current = block

        elif (
            len(current)
            + len(block)
            + 3
            <= MAX_MESSAGE_LENGTH
        ):

            current += (
                "\n\n\n"
                + block
            )

        else:

            await context.bot.send_message(
                chat_id=chat_id,
                text=current,
                parse_mode="HTML"
            )

            current = block

    if current:

        await context.bot.send_message(
            chat_id=chat_id,
            text=current,
            parse_mode="HTML",
            reply_markup=get_result_keyboard()
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

    user_id = (
        query.from_user.id
    )

    action = query.data

    # =====================================================
    # ГЛАВНОЕ МЕНЮ
    # =====================================================

    if action == "menu_main":

        await query.answer()

        if not is_authorized(user_id):

            await query.message.edit_text(
                "⛔ У тебя нет доступа."
            )

            return

        await show_main_menu(
            query.message,
            user_id,
            edit=True
        )

        return

    # =====================================================
    # НАЧАТЬ ВВОД
    # =====================================================

    if action in (
        "menu_start",
        "menu_new"
    ):

        await query.answer()

        if not is_authorized(user_id):

            await query.answer(
                "⛔ Нет доступа.",
                show_alert=True
            )

            return

        await start_input(
            query.message,
            user_id,
            context
        )

        return

    # =====================================================
    # ПОМОЩЬ
    # =====================================================

    if action == "menu_help":

        await query.answer()

        await show_help(
            query.message,
            edit=True
        )

        return

    # =====================================================
    # ПАРОЛИ
    # =====================================================

    if action == "menu_keys":

        await query.answer()

        if not is_owner(user_id):

            await query.answer(
                "⛔ Нет доступа.",
                show_alert=True
            )

            return

        result = make_password_list()

        await query.message.edit_text(
            result,
            parse_mode="HTML",
            reply_markup=get_back_keyboard()
        )

        return

    # =====================================================
    # СТАТИСТИКА
    # =====================================================

    if action == "menu_stats":

        await query.answer()

        if not is_owner(user_id):

            await query.answer(
                "⛔ Нет доступа.",
                show_alert=True
            )

            return

        await show_stats(
            query.message,
            edit=True
        )

        return

    # =====================================================
    # ПОСЛЕДНИЕ ОТВЕТЫ
    # =====================================================

    if action == "menu_last":

        await query.answer()

        if not is_authorized(user_id):

            await query.answer(
                "⛔ Нет доступа.",
                show_alert=True
            )

            return

        text = user_last_texts.get(
            user_id
        )

        if not text:

            await query.message.edit_text(
                "📋 <b>Последних ответов пока нет.</b>\n\n"
                "Сначала обработай какой-нибудь "
                "набор заданий.",
                parse_mode="HTML",
                reply_markup=get_back_keyboard()
            )

            return

        questions = parse_questions(
            text
        )

        if not questions:

            await query.message.edit_text(
                "❌ Не удалось восстановить "
                "последние ответы.",
                reply_markup=get_back_keyboard()
            )

            return

        result = make_mobile(
            questions
        )

        if len(result) <= MAX_MESSAGE_LENGTH:

            await query.message.edit_text(
                result,
                parse_mode="HTML",
                reply_markup=get_result_keyboard()
            )

        else:

            await query.message.edit_text(
                "📋 Последние ответы слишком "
                "большие для одного сообщения.\n\n"
                "Отправь задания заново.",
                reply_markup=get_back_keyboard()
            )

        return

    # =====================================================
    # ЗАВЕРШИТЬ ВВОД
    # =====================================================

    if action == "finish_input":

        await finish_input(
            update,
            context
        )

        return

    # =====================================================
    # НИЖЕ — КНОПКИ ФОРМАТА
    # =====================================================

    await query.answer()

    if not is_authorized(user_id):

        await query.message.reply_text(
            "⛔ У тебя нет доступа к боту."
        )

        return

    text = user_last_texts.get(
        user_id
    )

    if not text:

        await query.message.reply_text(
            "⚠️ Исходные данные не найдены.\n\n"
            "Пришли задания ещё раз."
        )

        return

    try:

        questions = parse_questions(
            text
        )

        if not questions:

            await query.message.reply_text(
                "❌ Не удалось разобрать "
                "исходные данные."
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

            await query.message.edit_text(
                result,
                parse_mode="HTML",
                reply_markup=get_result_keyboard()
            )

        else:

            await send_long_message(
                query.message,
                result,
                reply_markup=get_result_keyboard()
            )

    except Exception as error:

        print(
            "Ошибка кнопки:",
            error
        )

        await query.message.reply_text(
            "❌ Не удалось изменить формат."
        )


# =========================================================
# ДЛИННОЕ СООБЩЕНИЕ
# =========================================================

async def send_long_message(
    message,
    text,
    reply_markup=None
):

    if len(text) <= MAX_MESSAGE_LENGTH:

        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup
        )

        return

    blocks = text.split(
        "\n\n\n"
    )

    current = ""

    for block in blocks:

        if not current:

            current = block

        elif (
            len(current)
            + len(block)
            + 3
            <= MAX_MESSAGE_LENGTH
        ):

            current += (
                "\n\n\n"
                + block
            )

        else:

            await message.reply_text(
                current,
                parse_mode="HTML"
            )

            current = block

    if current:

        await message.reply_text(
            current,
            parse_mode="HTML",
            reply_markup=reply_markup
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
# HEALTH CHECK
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
            "Не задана переменная BOT_TOKEN"
        )

    if not RENDER_URL:
        raise ValueError(
            "Не задана переменная RENDER_URL"
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

    initialize_passwords()

    await telegram_app.initialize()

    await telegram_app.start()

    await telegram_app.bot.set_webhook(
        url=f"{RENDER_URL}/telegram",
        allowed_updates=Update.ALL_TYPES
    )

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
