import os
import asyncio
import re
import html

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


# =========================================================
# НАСТРОЙКИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL")
PORT = int(os.environ.get("PORT", 10000))

MAX_MESSAGE_LENGTH = 3900


# =========================================================
# ХРАНЕНИЕ ПОСЛЕДНИХ СООБЩЕНИЙ
# =========================================================

user_texts = {}


# =========================================================
# РАЗБОР ВОПРОСОВ И ОТВЕТОВ
# =========================================================

def parse_questions(text: str):
    """
    Обрабатывает сообщения такого вида:

    [28.08.2026 15:59] user: Получил сообщение:

    Вопрос 1:
    Как называлась политика?

    Ответ:
    коренизация

    Вопрос 5:
    Выберите республики.

    Ответ:
    » РСФСР
    » Украинская ССР

    На выходе:

    [
        (1, "Как называлась политика?", "коренизация"),
        (5, "Выберите республики.", "» РСФСР\n» Украинская ССР")
    ]

    Порядок вопросов сохраняется!
    """

    text = text.strip()

    if not text:
        return []

    # Нормализуем переносы строк
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    # -----------------------------------------------------
    # Ищем каждый блок:
    #
    # Вопрос N:
    # ТЕКСТ ВОПРОСА
    #
    # Ответ:
    # ОТВЕТ
    #
    # До следующего "Вопрос N:".
    # -----------------------------------------------------

    pattern = re.compile(
        r"Вопрос\s+(\d+)\s*:\s*"
        r"(.*?)"
        r"\bОтвет\s*:\s*"
        r"(.*?)"
        r"(?=\n\s*Вопрос\s+\d+\s*:|\Z)",
        re.IGNORECASE | re.DOTALL
    )

    matches = pattern.finditer(text)

    questions = []

    for match in matches:

        number = int(match.group(1))

        question = match.group(2).strip()
        answer = match.group(3).strip()

        # -------------------------------------------------
        # Очищаем вопрос
        # -------------------------------------------------

        question = re.sub(
            r"\n\s*",
            " ",
            question
        )

        question = re.sub(
            r"\s+",
            " ",
            question
        ).strip()

        # -------------------------------------------------
        # Очищаем ответ
        # -------------------------------------------------

        # Убираем пробелы в конце строк,
        # но сохраняем переносы.
        answer = "\n".join(
            line.rstrip()
            for line in answer.split("\n")
        )

        # Убираем лишние пустые строки
        answer = re.sub(
            r"\n\s*\n+",
            "\n",
            answer
        ).strip()

        if question and answer:

            questions.append(
                (
                    number,
                    question,
                    answer
                )
            )

    # -----------------------------------------------------
    # ВАЖНО:
    #
    # Здесь НЕТ сортировки.
    #
    # Порядок остаётся таким, как во входном сообщении.
    # -----------------------------------------------------

    return questions


# =========================================================
# ФОРМАТ ДЛЯ ТЕЛЕФОНА
# =========================================================

def make_mobile(questions):
    """
    Создаёт удобный для телефона формат:

    📝 ОТВЕТЫ

    ❓ 5. Назовите республики...

    ✅ РСФСР
       Украинская ССР


    ❓ 2. В каком году образовался СССР?

    ✅ 1922
    """

    if not questions:
        return "❌ Не удалось найти вопросы и ответы."

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

        # Для ответов со списком сохраняем
        # нормальные переносы.
        answer_lines = safe_answer.split("\n")

        formatted_answer = []

        for index, line in enumerate(answer_lines):

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

        parts.append(block)

    return "\n\n\n".join(parts)


# =========================================================
# КОМПАКТНЫЙ СПИСОК
# =========================================================

def make_list(questions):
    """
    Компактный формат:

    5. Назовите республики...
    → РСФСР, Украинская ССР

    2. В каком году образовался СССР?
    → 1922
    """

    if not questions:
        return "❌ Не удалось найти вопросы и ответы."

    parts = []

    for number, question, answer in questions:

        safe_question = html.escape(
            question
        )

        safe_answer = html.escape(
            answer.replace("\n", " ")
        )

        safe_answer = re.sub(
            r"\s+",
            " ",
            safe_answer
        ).strip()

        parts.append(
            f"<b>{number}. "
            f"{safe_question}</b>\n"
            f"→ {safe_answer}"
        )

    return "\n\n".join(parts)


# =========================================================
# ТОЛЬКО ОТВЕТЫ
# =========================================================

def make_compact(questions):
    """
    Показывает только:

    5. РСФСР, УССР, БССР
    2. 1922
    8. коренизация

    Номер сохраняется!
    """

    if not questions:
        return "❌ Не удалось найти вопросы и ответы."

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
        ).strip()

        parts.append(
            f"<b>{number}.</b> "
            f"{html.escape(clean_answer)}"
        )

    return "\n".join(parts)


# =========================================================
# КНОПКИ
# =========================================================

def get_keyboard():

    keyboard = [

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

    return InlineKeyboardMarkup(
        keyboard
    )


# =========================================================
# ОТПРАВКА ДЛИННЫХ СООБЩЕНИЙ
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

    # Разбиваем по блокам.
    blocks = text.split("\n\n\n")

    current = ""

    for block in blocks:

        if not current:

            current = block

        elif len(current) + len(block) + 3 <= MAX_MESSAGE_LENGTH:

            current += "\n\n\n" + block

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
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Пришли мне текст с вопросами и ответами.\n\n"
        "Я автоматически уберу дату, имя, "
        "\"Получил сообщение:\" и всё остальное, "
        "оставив вопрос и его ответ.\n\n"
        "Порядок вопросов сохранится."
    )


# =========================================================
# ОБРАБОТКА НОВОГО СООБЩЕНИЯ
# =========================================================

async def echo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.text:
        return

    text = update.message.text

    user_id = update.effective_user.id

    # Запоминаем исходный текст
    user_texts[user_id] = text

    try:

        questions = parse_questions(text)

        if not questions:

            await update.message.reply_text(
                "❌ Я не смог найти пары "
                "\"Вопрос → Ответ\".\n\n"
                "Проверь, что в тексте есть:\n"
                "Вопрос 1:\n"
                "...\n"
                "Ответ:\n"
                "..."
            )

            return

        result = make_mobile(
            questions
        )

        await send_long_message(
            update.message,
            result,
            reply_markup=get_keyboard()
        )

    except Exception as error:

        print(
            f"Ошибка обработки: {error}"
        )

        await update.message.reply_text(
            "❌ Произошла ошибка при обработке."
        )


# =========================================================
# ОБРАБОТКА КНОПОК
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user_id = query.from_user.id

    text = user_texts.get(
        user_id
    )

    if not text:

        await query.message.reply_text(
            "⚠️ Исходный текст больше не найден.\n"
            "Пришли его ещё раз."
        )

        return

    try:

        questions = parse_questions(
            text
        )

        if not questions:

            await query.message.reply_text(
                "❌ Не удалось разобрать "
                "исходный текст."
            )

            return

        action = query.data

        # -------------------------------------------------
        # УДОБНЫЙ ФОРМАТ
        # -------------------------------------------------

        if action == "mobile":

            result = make_mobile(
                questions
            )

        # -------------------------------------------------
        # СПИСОК
        # -------------------------------------------------

        elif action == "list":

            result = make_list(
                questions
            )

        # -------------------------------------------------
        # ТОЛЬКО ОТВЕТЫ
        # -------------------------------------------------

        elif action == "compact":

            result = make_compact(
                questions
            )

        # -------------------------------------------------
        # ЗАНОВО
        # -------------------------------------------------

        elif action == "repeat":

            result = make_mobile(
                questions
            )

        else:

            return

        await send_long_message(
            query.message,
            result,
            reply_markup=get_keyboard()
        )

    except Exception as error:

        print(
            f"Ошибка кнопки: {error}"
        )

        await query.message.reply_text(
            "❌ Не удалось изменить формат."
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
