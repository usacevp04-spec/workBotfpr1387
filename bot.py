import os
import asyncio

import uvicorn

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BOT_TOKEN = os.getenv("BOT_TOKEN")

# URL твоего сервиса Render, добавим позже
RENDER_URL = os.getenv("RENDER_URL")

# Порт Render
PORT = int(os.environ.get("PORT", 10000))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Пришли мне текст с ответами, и я помогу красиво его оформить."
    )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        text = update.message.text

        await update.message.reply_text(
            f"Получил сообщение:\n\n{text}"
        )


# Создаём Telegram-приложение без polling
telegram_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, echo)
)


async def telegram_webhook(request: Request) -> Response:
    data = await request.json()

    update = Update.de_json(data, telegram_app.bot)

    await telegram_app.update_queue.put(update)

    return Response("OK")


async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("Bot is running!")


web_app = Starlette(
    routes=[
        Route("/", health),
        Route("/health", health),
        Route("/telegram", telegram_webhook, methods=["POST"]),
    ]
)


async def main():
    if not BOT_TOKEN:
        raise ValueError("Не задана переменная BOT_TOKEN")

    if not RENDER_URL:
        raise ValueError("Не задана переменная RENDER_URL")

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

    print(f"Бот запущен на порту {PORT}")
    print(f"Webhook: {RENDER_URL}/telegram")

    await server.serve()

    await telegram_app.stop()
    await telegram_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
