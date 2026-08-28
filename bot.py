import os
import asyncio
import re
import textwrap
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

TABLE_WIDTH = 48


# =========================================================
# ХРАНЕНИЕ ПОСЛЕДНИХ СООБЩЕНИЙ
# =========================================================

# Здесь бот хранит последнее сообщение каждого пользователя.
# Это нужно для кнопок "Таблица", "Список" и т.д.
user_texts = {}


# =========================================================
# РАЗБОР ОТВЕТОВ
# =========================================================

def parse_answers(text: str):
    """
    Распознаёт:

    1. Ответ
    1) Ответ
    1 - Ответ
    1 — Ответ
    1: Ответ
    1 Ответ

    Поддерживает многострочные ответы.
    """

    text = text.strip()

    if not text:
        return []

    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    # Ищем начало нумерованного пункта.
    #
    # ВАЖНО:
    # Нумерация должна начинаться с новой строки.
    pattern = re.compile(
        r"(?m)^\s*(\d+)\s*(?:[.)]|[-–—:]|\s)\s*(.*)$"
    )

    matches = list(pattern.finditer(text))

    # -----------------------------------------------------
    # Нумерация найдена
    # -----------------------------------------------------

    if matches:

        answers = []

        for i, match in enumerate(matches):

            number = int(match.group(1))
            first_line = match.group(2).strip()

            start = match.end()

            if i + 1 < len(matches):
                end = matches[i + 1].start()
            else:
                end = len(text)

            continuation = text[start:end].strip()

            if continuation:

                if first_line:
                    answer = first_line + "\n" + continuation
                else:
                    answer = continuation

            else:
                answer = first_line

            # Нормализуем пробелы
            answer = re.sub(
                r"[ \t]+",
                " ",
                answer
            )

            # Убираем лишние пустые строки
            answer = re.sub(
                r"\n\s*\n+",
                "\n",
                answer
            )

            if answer:
                answers.append(
                    (number, answer)
                )

        if answers:
            return answers

    # -----------------------------------------------------
    # Нумерации нет
    # -----------------------------------------------------

    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]

    if not lines:
        return []

    return [
        (index, line)
        for index, line in enumerate(
            lines,
            start=1
        )
    ]


# =========================================================
# ПЕРЕНОС СТРОК
# =========================================================

def wrap_answer(answer: str, width: int):

    result = []

    for line in answer.split("\n"):

        wrapped = textwrap.wrap(
            line,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        )

        if wrapped:
            result.extend(wrapped)
        else:
            result.append("")

    return result


# =========================================================
# СОЗДАНИЕ ТАБЛИЦЫ
# =========================================================

def make_table(answers):

    if not answers:
        return "❌ Не удалось найти ответы."

    number_width = max(
        len(str(number))
        for number, _ in answers
    )

    answer_width = (
        TABLE_WIDTH
        - number_width
        - 7
    )

    answer_width = max(
        answer_width,
        15
    )

    top = (
        "┌"
        + "─" * (number_width + 2)
        + "┬"
        + "─" * (answer_width + 2)
        + "┐"
    )

    header = (
        "│ "
        + "№".center(number_width)
        + " │ "
        + "Ответ".ljust(answer_width)
        + " │"
    )

    separator = (
        "├"
        + "─" * (number_width + 2)
        + "┼"
        + "─" * (answer_width + 2)
        + "┤"
    )

    bottom = (
        "└"
        + "─" * (number_width + 2)
        + "┴"
        + "─" * (answer_width + 2)
        + "┘"
    )

    rows = [
        top,
        header,
        separator,
    ]

    for answer_index, (number, answer) in enumerate(answers):

        wrapped = wrap_answer(
            answer,
            answer_width
        )

        for line_index, line in enumerate(wrapped):

            if line_index == 0:
                number_text = str(number).center(
                    number_width
                )
            else:
                number_text = " " * number_width

            row = (
                "│ "
                + number_text
                + " │ "
                + line.ljust(answer_width)
                + " │"
            )

            rows.append(row)

        # Разделитель
        if answer_index < len(answers) - 1:

            rows.append(
                "├"
                + "─" * (number_width + 2)
                + "┼"
                + "─" * (answer_width + 2)
                + "┤"
            )

    rows.append(bottom)

    return "\n".join(rows)


# =========================================================
# ОБЫЧНЫЙ СПИСОК
# =========================================================

def make_list(answers):

    if not answers:
        return "❌ Не удалось найти ответы."

    result = []

    for number, answer in answers:

        result.append(
            f"{number}. {answer}"
        )

    return "\n\n".join(result)


# =========================================================
# ТОЛЬКО ОТВЕТЫ
# =========================================================

def make_compact(answers):

    if not answers:
        return "❌ Не удалось найти ответы."

    result = []

    for number, answer in answers:

        # Переносы строк превращаем в пробелы
        clean_answer = answer.replace(
            "\n",
            " "
        )

        clean_answer = re.sub(
            r"\s+",
            " ",
            clean_answer
        ).strip()

        result.append(
            f"{number}. {clean_answer}"
        )

    return " ".join(result)


# =========================================================
# КНОПКИ
# =========================================================

def get_keyboard():

    keyboard = [

        [
            InlineKeyboardButton(
                "📊 Таблица",
                callback_data="table"
            ),
            InlineKeyboardButton(
                "📝 Список",
                callback_data="list"
            ),
        ],

        [
            InlineKeyboardButton(
                "🔢 Компактно",
                callback_data="compact"
            ),
            InlineKeyboardButton(
                "🔄 Обработать заново",
                callback_data="repeat"
            ),
        ],

    ]

    return InlineKeyboardMarkup(
        keyboard
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
        "Пришли мне ответы — я автоматически "
        "распознаю их и оформлю.\n\n"
        "Например:\n\n"
        "1) Москва\n"
        "2) Санкт-Петербург\n"
        "3) Казань\n"
        "4) Новосибирск\n\n"
        "После этого ты сможешь выбрать нужный формат."
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

        answers = parse_answers(text)

        if not answers:

            await update.message.reply_text(
                "❌ Я не смог найти ответы."
            )

            return

        table = make_table(answers)

        safe_table = html.escape(table)

        await update.message.reply_text(
            f"<pre>{safe_table}</pre>",
            parse_mode="HTML",
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

    text = user_texts.get(user_id)

    # Если исходный текст не найден
    if not text:

        await query.message.reply_text(
            "⚠️ Я больше не вижу исходный текст.\n"
            "Пришли его ещё раз."
        )

        return

    try:

        answers = parse_answers(text)

        if not answers:

            await query.message.reply_text(
                "❌ Не удалось разобрать ответы."
            )

            return

        action = query.data

        # -------------------------------------------------
        # ТАБЛИЦА
        # -------------------------------------------------

        if action == "table":

            result = make_table(answers)

            await query.message.reply_text(
                f"<pre>{html.escape(result)}</pre>",
                parse_mode="HTML",
                reply_markup=get_keyboard()
            )

        # -------------------------------------------------
        # СПИСОК
        # -------------------------------------------------

        elif action == "list":

            result = make_list(answers)

            await query.message.reply_text(
                result,
                reply_markup=get_keyboard()
            )

        # -------------------------------------------------
        # КОМПАКТНО
        # -------------------------------------------------

        elif action == "compact":

            result = make_compact(answers)

            await query.message.reply_text(
                result,
                reply_markup=get_keyboard()
            )

        # -------------------------------------------------
        # ПОВТОРНАЯ ОБРАБОТКА
        # -------------------------------------------------

        elif action == "repeat":

            result = make_table(answers)

            await query.message.reply_text(
                f"<pre>{html.escape(result)}</pre>",
                parse_mode="HTML",
                reply_markup=get_keyboard()
            )

    except Exception as error:

        print(
            f"Ошибка кнопки: {error}"
        )

        await query.message.reply_text(
            "❌ Не удалось выполнить действие."
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
# START
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
