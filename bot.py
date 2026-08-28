import os
import asyncio

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Пришли мне текст с ответами, и я помогу красиво его оформить."
    )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    await update.message.reply_text(
        f"Получил сообщение:\n\n{text}"
    )


async def main():
    if not BOT_TOKEN:
        raise ValueError("Не задана переменная BOT_TOKEN")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, echo)
    )

    print("Бот запущен!")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Бот работает постоянно
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
