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
# ПАМЯТЬ
# =========================================================

# Все сообщения, которые пользователь отправил
# до нажатия "Завершить ввод"
user_buffers = {}

# ID последнего сообщения пользователя,
# на котором сейчас находится кнопка
user_last_control_message = {}

# Последний готовый набор вопросов и ответов
user_last_texts = {}


# =========================================================
# КНОПКА "ЗАВЕРШИТЬ ВВОД"
# =========================================================

def get_finish_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "⏹ Завершить ввод",
                callback_data="finish_input"
            )
        ]
    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# =========================================================
# КНОПКИ РЕЗУЛЬТАТА
# =========================================================

def get_result_keyboard():

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
# РАЗБОР ВОПРОСОВ И ОТВЕТОВ
# =========================================================

def parse_questions(text: str):

    text = text.strip()

    if not text:
        return []

    # Нормализуем переносы строк
    text = text.replace(
        "\r\n",
        "\n"
    )

    text = text.replace(
        "\r",
        "\n"
    )

    # -----------------------------------------------------
    # Ищем конструкцию:
    #
    # Вопрос 1:
    # Текст вопроса
    #
    # Ответ:
    # Ответ
    #
    # До следующего Вопрос N:
    # -----------------------------------------------------

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

        # -------------------------------------------------
        # Очистка вопроса
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
        )

        question = question.strip()

        # -------------------------------------------------
        # Очистка ответа
        # -------------------------------------------------

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

    # ВАЖНО:
    # Никакой сортировки нет.
    #
    # Порядок вопросов остаётся таким,
    # каким они были отправлены пользователем.

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

        answer_lines = safe_answer.split(
            "\n"
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
# КОМПАКТНЫЙ ФОРМАТ
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
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user_id = update.effective_user.id

    # Начинаем новый набор
    user_buffers[user_id] = []

    user_last_control_message.pop(
        user_id,
        None
    )

    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Отправляй мне сообщения с вопросами "
        "и ответами.\n\n"
        "Можно отправить одно или много сообщений.\n\n"
        "Когда закончишь — нажми "
        "«⏹ Завершить ввод» "
        "на последнем сообщении."
    )


# =========================================================
# ПОЛУЧЕНИЕ НОВОГО СООБЩЕНИЯ
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

    chat_id = update.effective_chat.id

    # -----------------------------------------------------
    # Создаём буфер
    # -----------------------------------------------------

    if user_id not in user_buffers:

        user_buffers[user_id] = []

    # -----------------------------------------------------
    # Добавляем новое сообщение
    # -----------------------------------------------------

    user_buffers[user_id].append(
        text
    )

    # -----------------------------------------------------
    # Убираем кнопку со старого последнего
    # сообщения бота
    # -----------------------------------------------------

    old_control_message_id = (
        user_last_control_message.get(
            user_id
        )
    )

    if old_control_message_id:

        try:

            await context.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=old_control_message_id,
                reply_markup=None
            )

        except Exception as error:

            print(
                "Не удалось убрать старую кнопку:",
                error
            )

    # -----------------------------------------------------
    # Создаём новое сообщение с кнопкой
    # -----------------------------------------------------

    control_message = (
        await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Ответы собираются...",
            reply_markup=get_finish_keyboard()
        )
    )

    # Запоминаем его как последнее
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

    await query.answer(
        "Обрабатываю..."
    )

    user_id = query.from_user.id

    chat_id = query.message.chat_id

    # -----------------------------------------------------
    # Получаем все накопленные сообщения
    # -----------------------------------------------------

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
    # Объединяем
    # -----------------------------------------------------

    combined_text = "\n\n".join(
        messages
    )

    # -----------------------------------------------------
    # Разбираем
    # -----------------------------------------------------

    questions = parse_questions(
        combined_text
    )

    if not questions:

        await query.message.edit_text(
            "❌ Я не смог найти пары "
            "«Вопрос → Ответ».\n\n"
            "Проверь формат входных сообщений."
        )

        user_buffers[user_id] = []

        return

    # -----------------------------------------------------
    # Сохраняем последний результат
    # -----------------------------------------------------

    user_last_texts[user_id] = (
        combined_text
    )

    # -----------------------------------------------------
    # Очищаем буфер
    # -----------------------------------------------------

    user_buffers[user_id] = []

    user_last_control_message.pop(
        user_id,
        None
    )

    # -----------------------------------------------------
    # Создаём результат
    # -----------------------------------------------------

    result = make_mobile(
        questions
    )

    # -----------------------------------------------------
    # Пытаемся превратить сообщение
    # с кнопкой в результат
    # -----------------------------------------------------

    if len(result) <= MAX_MESSAGE_LENGTH:

        await query.message.edit_text(
            result,
            parse_mode="HTML",
            reply_markup=get_result_keyboard()
        )

    else:

        # Если слишком много текста,
        # удаляем сообщение с кнопкой
        # и отправляем части.

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
# ОБРАБОТКА КНОПОК
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    # -----------------------------------------------------
    # Кнопка "Завершить ввод"
    # -----------------------------------------------------

    if query.data == "finish_input":

        await finish_input(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # Остальные кнопки
    # -----------------------------------------------------

    await query.answer()

    user_id = query.from_user.id

    text = user_last_texts.get(
        user_id
    )

    if not text:

        await query.message.reply_text(
            "⚠️ Исходные данные больше "
            "не найдены.\n\n"
            "Пришли сообщения ещё раз."
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

        action = query.data

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

        # -------------------------------------------------
        # Обновляем текущее сообщение,
        # вместо создания нового
        # -------------------------------------------------

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
