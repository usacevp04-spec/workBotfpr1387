import os
import re
import html
import hashlib
import secrets
import asyncio
import json

import requests
import uvicorn

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, HTMLResponse

from telegram import (
    Update,
    ReplyKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from supabase import create_client


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

OWNER_ID = os.getenv("OWNER_ID")

PORT = int(
    os.getenv(
        "PORT",
        "10000"
    )
)

SKYSMART_API = (
    "https://skysmart-answers.vercel.app/get_answers/"
)

MAX_MESSAGE_LENGTH = 3900


# ============================================================
# ПРОВЕРКА НАСТРОЕК
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not RENDER_URL:
    raise RuntimeError("Не задан RENDER_URL")

if not SUPABASE_URL:
    raise RuntimeError("Не задан SUPABASE_URL")

if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("Не задан SUPABASE_SERVICE_KEY")

if not OWNER_ID:
    raise RuntimeError("Не задан OWNER_ID")


OWNER_ID = int(OWNER_ID)


# ============================================================
# SUPABASE
# ============================================================

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


# ============================================================
# МЕНЮ
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [
            "📚 Skysmart",
            "📄 Google Forms"
        ],
        [
            "ℹ️ Помощь"
        ],
    ],
    resize_keyboard=True,
)


# ============================================================
# РЕЖИМЫ
# ============================================================

user_modes = {}


def get_user_mode(user_id: int) -> str:

    return user_modes.get(
        user_id,
        "skysmart"
    )


def set_user_mode(
    user_id: int,
    mode: str
):

    user_modes[user_id] = mode


# ============================================================
# ПАРОЛИ
# ============================================================

def hash_password(password: str) -> str:

    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def generate_password() -> str:

    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
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


def initialize_passwords():

    try:

        result = (
            supabase
            .table("bot_passwords")
            .select("id")
            .eq("used", False)
            .execute()
        )

        current_count = len(
            result.data or []
        )

        while current_count < 10:

            password = generate_password()

            password_hash = hash_password(
                password
            )

            existing = (
                supabase
                .table("bot_passwords")
                .select("id")
                .eq(
                    "password_hash",
                    password_hash
                )
                .execute()
            )

            if existing.data:
                continue

            (
                supabase
                .table("bot_passwords")
                .insert({
                    "password_hash": password_hash,
                    "password_text": password,
                    "used": False,
                    "used_by": None,
                    "used_at": None,
                })
                .execute()
            )

            current_count += 1

        print(
            f"Пароли: свободных {current_count}"
        )

    except Exception as e:

        print(
            f"Ошибка создания паролей: {e}"
        )


# ============================================================
# ПОЛЬЗОВАТЕЛИ
# ============================================================

def is_authorized(user_id: int) -> bool:

    try:

        result = (
            supabase
            .table("bot_users")
            .select("authorized")
            .eq(
                "telegram_id",
                user_id
            )
            .limit(1)
            .execute()
        )

        if not result.data:
            return False

        return bool(
            result.data[0].get(
                "authorized"
            )
        )

    except Exception as e:

        print(
            f"Ошибка проверки пользователя: {e}"
        )

        return False


def create_user_if_needed(user_id: int):

    try:

        result = (
            supabase
            .table("bot_users")
            .select("telegram_id")
            .eq(
                "telegram_id",
                user_id
            )
            .limit(1)
            .execute()
        )

        if not result.data:

            (
                supabase
                .table("bot_users")
                .insert({
                    "telegram_id": user_id,
                    "authorized": False,
                })
                .execute()
            )

    except Exception as e:

        print(
            f"Ошибка создания пользователя: {e}"
        )


def use_password(
    password: str,
    user_id: int
) -> bool:

    password = (
        password
        .strip()
        .upper()
    )

    password_hash = hash_password(
        password
    )

    try:

        result = (
            supabase
            .table("bot_passwords")
            .select("*")
            .eq(
                "password_hash",
                password_hash
            )
            .eq(
                "used",
                False
            )
            .limit(1)
            .execute()
        )

        if not result.data:
            return False

        password_row = result.data[0]

        (
            supabase
            .table("bot_passwords")
            .update({
                "used": True,
                "used_by": user_id,
            })
            .eq(
                "id",
                password_row["id"]
            )
            .execute()
        )

        (
            supabase
            .table("bot_users")
            .update({
                "authorized": True,
            })
            .eq(
                "telegram_id",
                user_id
            )
            .execute()
        )

        return True

    except Exception as e:

        print(
            f"Ошибка использования пароля: {e}"
        )

        return False


# ============================================================
# GOOGLE FORM URL
# ============================================================

def extract_google_form_url(text: str):

    if not text:
        return None

    # --------------------------------------------------------
    # /d/e/PUBLIC_ID/viewform
    # --------------------------------------------------------

    match = re.search(
        r"https?://docs\.google\.com/forms/d/e/"
        r"[a-zA-Z0-9_-]+"
        r"(?:/[^?\s]*)?",
        text,
        flags=re.IGNORECASE
    )

    if match:

        url = match.group(0)

        url = url.rstrip(
            ".,!?)]}>"
        )

        return url

    # --------------------------------------------------------
    # /d/FORM_ID/viewform
    # --------------------------------------------------------

    match = re.search(
        r"https?://docs\.google\.com/forms/d/"
        r"(?!e(?:/|$))"
        r"[a-zA-Z0-9_-]+"
        r"(?:/[^?\s]*)?",
        text,
        flags=re.IGNORECASE
    )

    if match:

        url = match.group(0)

        url = url.rstrip(
            ".,!?)]}>"
        )

        # Если прислали /edit, пытаемся заменить
        # на публичную страницу.
        url = re.sub(
            r"/edit(?:[/?#].*)?$",
            "/viewform",
            url,
            flags=re.IGNORECASE
        )

        return url

    return None


# ============================================================
# GOOGLE FORMS HTML
# ============================================================

GOOGLE_FORMS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def fetch_google_form_html(
    url: str
) -> str:

    response = requests.get(
        url,
        headers=GOOGLE_FORMS_HEADERS,
        timeout=30,
        allow_redirects=True
    )

    response.raise_for_status()

    if not response.text:
        raise RuntimeError(
            "Google Form вернула пустую страницу."
        )

    return response.text


# ============================================================
# ПОИСК FB_PUBLIC_LOAD_DATA_
# ============================================================

def extract_fb_public_load_data(
    page_html: str
):

    if not page_html:
        raise RuntimeError(
            "Пустой HTML Google Form."
        )

    marker = "FB_PUBLIC_LOAD_DATA_"

    marker_position = page_html.find(
        marker
    )

    if marker_position == -1:

        raise RuntimeError(
            "Не удалось найти данные Google Form "
            "в HTML страницы."
        )

    # --------------------------------------------------------
    # Ищем первую "[" после FB_PUBLIC_LOAD_DATA_
    # --------------------------------------------------------

    start = page_html.find(
        "[",
        marker_position
    )

    if start == -1:

        raise RuntimeError(
            "Структура Google Form повреждена "
            "или изменилась."
        )

    # --------------------------------------------------------
    # JSONDecoder.raw_decode умеет прочитать
    # JSON-массив из начала строки, даже если
    # после него идут дополнительные символы.
    # --------------------------------------------------------

    decoder = json.JSONDecoder()

    try:

        data, end_position = (
            decoder.raw_decode(
                page_html[start:]
            )
        )

    except json.JSONDecodeError as e:

        print(
            "JSON decode error:",
            e
        )

        # ----------------------------------------------------
        # Запасной вариант:
        # пытаемся найти конец JSON перед </script>
        # ----------------------------------------------------

        script_end = page_html.find(
            "</script>",
            start
        )

        if script_end == -1:

            raise RuntimeError(
                "Не удалось разобрать структуру "
                "Google Form."
            )

        raw = page_html[
            start:script_end
        ].strip()

        # Иногда в конце стоит ;
        raw = raw.rstrip(
            "; \r\n\t"
        )

        try:

            data = json.loads(
                raw
            )

        except Exception as json_error:

            print(
                "Fallback JSON error:",
                json_error
            )

            raise RuntimeError(
                "Google Form использует формат, "
                "который бот пока не смог разобрать."
            )

    if not isinstance(
        data,
        list
    ):

        raise RuntimeError(
            "Google Form вернула некорректную структуру."
        )

    return data


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ GOOGLE FORMS
# ============================================================

def safe_get(
    value,
    index,
    default=None
):

    if not isinstance(
        value,
        list
    ):

        return default

    if index < 0 or index >= len(value):

        return default

    return value[index]


def clean_google_text(
    value
) -> str:

    if value is None:
        return ""

    if not isinstance(
        value,
        str
    ):

        value = str(value)

    value = html.unescape(
        value
    )

    value = value.replace(
        "\r\n",
        "\n"
    )

    value = value.replace(
        "\r",
        "\n"
    )

    # Убираем слишком много пустых строк
    value = re.sub(
        r"\n{3,}",
        "\n\n",
        value
    )

    return value.strip()


def get_google_form_meta(
    data
):

    title = ""
    description = ""

    second = safe_get(
        data,
        1,
        []
    )

    if isinstance(
        second,
        list
    ):

        # Описание
        description = clean_google_text(
            safe_get(
                second,
                0,
                ""
            )
        )

        # Заголовок
        title = clean_google_text(
            safe_get(
                second,
                8,
                ""
            )
        )

    if not title:
        title = "Google Forms"

    return title, description


# ============================================================
# GOOGLE FORMS QUESTION PARSER
# ============================================================

def parse_google_form_items(
    data
):

    second = safe_get(
        data,
        1,
        []
    )

    if not isinstance(
        second,
        list
    ):

        return []

    items = safe_get(
        second,
        1,
        []
    )

    if not isinstance(
        items,
        list
    ):

        return []

    parsed_items = []

    for raw_item in items:

        if not isinstance(
            raw_item,
            list
        ):

            continue

        # ----------------------------------------------------
        # Структура:
        #
        # [internal_id, title, description, type, ...]
        # ----------------------------------------------------

        item_title = clean_google_text(
            safe_get(
                raw_item,
                1,
                ""
            )
        )

        item_description = clean_google_text(
            safe_get(
                raw_item,
                2,
                ""
            )
        )

        item_type = safe_get(
            raw_item,
            3,
            None
        )

        item_sub = safe_get(
            raw_item,
            4,
            []
        )

        if not isinstance(
            item_sub,
            list
        ):

            item_sub = []

        parsed = {
            "title": item_title,
            "description": item_description,
            "type": item_type,
            "options": [],
            "required": False,
            "rows": [],
            "columns": [],
        }

        # ----------------------------------------------------
        # Вытаскиваем данные из sub-массивов
        # ----------------------------------------------------

        for sub in item_sub:

            if not isinstance(
                sub,
                list
            ):

                continue

            # required обычно находится здесь
            if len(sub) > 2:

                if sub[2] in (
                    True,
                    1
                ):

                    parsed["required"] = True

            # options
            option_data = safe_get(
                sub,
                1,
                None
            )

            if isinstance(
                option_data,
                list
            ):

                for option in option_data:

                    if isinstance(
                        option,
                        list
                    ) and option:

                        value = clean_google_text(
                            option[0]
                        )

                        if value:

                            parsed[
                                "options"
                            ].append(
                                value
                            )

        # ----------------------------------------------------
        # ШКАЛА
        # ----------------------------------------------------

        if item_type == 5:

            first_sub = safe_get(
                item_sub,
                0,
                []
            )

            if isinstance(
                first_sub,
                list
            ):

                scale_values = safe_get(
                    first_sub,
                    1,
                    []
                )

                if isinstance(
                    scale_values,
                    list
                ):

                    low = safe_get(
                        scale_values,
                        0,
                        ""
                    )

                    high = safe_get(
                        scale_values,
                        -1,
                        ""
                    )

                    # Обычно labels находятся после
                    # диапазона.
                    labels = []

                    for value in scale_values:

                        if isinstance(
                            value,
                            str
                        ):

                            labels.append(
                                clean_google_text(
                                    value
                                )
                            )

                    parsed["scale_low"] = low
                    parsed["scale_high"] = high

                    if len(labels) >= 2:

                        parsed[
                            "scale_low_label"
                        ] = labels[-2]

                        parsed[
                            "scale_high_label"
                        ] = labels[-1]

        # ----------------------------------------------------
        # GRID
        # ----------------------------------------------------

        if item_type in (
            7,
            11
        ):

            for sub in item_sub:

                if not isinstance(
                    sub,
                    list
                ):

                    continue

                options = safe_get(
                    sub,
                    1,
                    []
                )

                if not isinstance(
                    options,
                    list
                ):

                    continue

                values = []

                for option in options:

                    if isinstance(
                        option,
                        list
                    ) and option:

                        value = clean_google_text(
                            option[0]
                        )

                        if value:
                            values.append(
                                value
                            )

                if values:

                    if not parsed["rows"]:

                        parsed[
                            "rows"
                        ] = values

                    else:

                        parsed[
                            "columns"
                        ] = values

        # ----------------------------------------------------
        # Удаляем дубли вариантов
        # ----------------------------------------------------

        unique_options = []

        for option in parsed["options"]:

            if option not in unique_options:

                unique_options.append(
                    option
                )

        parsed["options"] = unique_options

        # ----------------------------------------------------
        # Сохраняем только элементы с содержимым.
        # Разделы тоже могут иметь title.
        # ----------------------------------------------------

        if (
            item_title
            or parsed["options"]
            or item_type in (
                0,
                1,
                2,
                3,
                4,
                5,
                7,
                9,
                10,
                11,
            )
        ):

            parsed_items.append(
                parsed
            )

    return parsed_items


# ============================================================
# GOOGLE FORM FORMAT
# ============================================================

def format_google_form(
    data
):

    title, description = (
        get_google_form_meta(
            data
        )
    )

    items = parse_google_form_items(
        data
    )

    lines = []

    lines.append(
        "📄 <b>Google Forms</b>"
    )

    lines.append(
        f"📝 <b>{html.escape(title)}</b>"
    )

    if description:

        clean_description = re.sub(
            r"\s+",
            " ",
            description
        ).strip()

        if clean_description:

            lines.append(
                "ℹ️ "
                + html.escape(
                    clean_description
                )
            )

    lines.append("")

    question_number = 0

    for item in items:

        item_type = item.get(
            "type"
        )

        item_title = item.get(
            "title",
            ""
        )

        item_description = item.get(
            "description",
            ""
        )

        # ----------------------------------------------------
        # Раздел / заголовок секции
        # ----------------------------------------------------

        if item_type in (
            8,
            6
        ):

            if item_title:

                lines.append(
                    "📌 <b>"
                    + html.escape(
                        item_title
                    )
                    + "</b>"
                )

            if item_description:

                lines.append(
                    html.escape(
                        item_description
                    )
                )

            lines.append("")

            continue

        # ----------------------------------------------------
        # Обычный вопрос
        # ----------------------------------------------------

        question_number += 1

        if not item_title:

            item_title = "Без названия"

        required_text = ""

        if item.get(
            "required"
        ):

            required_text = " <i>(обязательный)</i>"

        lines.append(
            f"<b>{question_number}. "
            f"{html.escape(item_title)}</b>"
            f"{required_text}"
        )

        if item_description:

            lines.append(
                "   ℹ️ "
                + html.escape(
                    item_description
                )
            )

        # ----------------------------------------------------
        # ВАРИАНТЫ
        # ----------------------------------------------------

        options = item.get(
            "options",
            []
        )

        if options:

            for option in options:

                lines.append(
                    "   • "
                    + html.escape(
                        str(option)
                    )
                )

        # ----------------------------------------------------
        # ТЕКСТОВОЕ ПОЛЕ
        # ----------------------------------------------------

        if item_type == 0:

            lines.append(
                "   ✏️ "
                "<i>Краткий текстовый ответ</i>"
            )

        elif item_type == 1:

            lines.append(
                "   📝 "
                "<i>Развёрнутый текстовый ответ</i>"
            )

        # ----------------------------------------------------
        # CHECKBOX
        # ----------------------------------------------------

        elif item_type == 4:

            if not options:

                lines.append(
                    "   ☑️ "
                    "<i>Можно выбрать несколько вариантов</i>"
                )

        # ----------------------------------------------------
        # RADIO
        # ----------------------------------------------------

        elif item_type == 2:

            if not options:

                lines.append(
                    "   🔘 "
                    "<i>Один вариант ответа</i>"
                )

        # ----------------------------------------------------
        # DROPDOWN
        # ----------------------------------------------------

        elif item_type == 3:

            if not options:

                lines.append(
                    "   🔽 "
                    "<i>Выбор из списка</i>"
                )

        # ----------------------------------------------------
        # ШКАЛА
        # ----------------------------------------------------

        elif item_type == 5:

            low = item.get(
                "scale_low",
                ""
            )

            high = item.get(
                "scale_high",
                ""
            )

            scale_text = (
                f"   📊 <b>Шкала:</b> "
                f"{html.escape(str(low))}"
                f"–"
                f"{html.escape(str(high))}"
            )

            low_label = item.get(
                "scale_low_label",
                ""
            )

            high_label = item.get(
                "scale_high_label",
                ""
            )

            if low_label:

                scale_text += (
                    f" — "
                    f"{html.escape(str(low_label))}"
                )

            if high_label:

                scale_text += (
                    f" → "
                    f"{html.escape(str(high_label))}"
                )

            lines.append(
                scale_text
            )

        # ----------------------------------------------------
        # ДАТА
        # ----------------------------------------------------

        elif item_type == 9:

            lines.append(
                "   📅 "
                "<i>Поле для выбора даты</i>"
            )

        # ----------------------------------------------------
        # ВРЕМЯ
        # ----------------------------------------------------

        elif item_type == 10:

            lines.append(
                "   🕐 "
                "<i>Поле для выбора времени</i>"
            )

        # ----------------------------------------------------
        # GRID
        # ----------------------------------------------------

        elif item_type in (
            7,
            11
        ):

            rows = item.get(
                "rows",
                []
            )

            columns = item.get(
                "columns",
                []
            )

            if rows:

                lines.append(
                    "   📋 <b>Строки:</b>"
                )

                for row in rows:

                    lines.append(
                        "      • "
                        + html.escape(
                            str(row)
                        )
                    )

            if columns:

                lines.append(
                    "   📊 <b>Варианты:</b>"
                )

                for column in columns:

                    lines.append(
                        "      • "
                        + html.escape(
                            str(column)
                        )
                    )

        lines.append("")

    if question_number == 0:

        return (
            "❌ <b>В форме не найдено вопросов.</b>\n\n"
            "Возможно, Google изменил структуру "
            "страницы или форма недоступна "
            "без авторизации."
        )

    return "\n".join(
        lines
    ).strip()


# ============================================================
# ПОЛУЧЕНИЕ GOOGLE FORM
# ============================================================

def get_google_form(
    form_url: str
):

    page_html = fetch_google_form_html(
        form_url
    )

    data = extract_fb_public_load_data(
        page_html
    )

    return data


# ============================================================
# /START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    user_id = update.effective_user.id

    create_user_if_needed(
        user_id
    )

    if is_authorized(
        user_id
    ):

        await update.message.reply_text(
            "🤖 <b>Добро пожаловать!</b>\n\n"
            "Выберите режим на клавиатуре ниже:",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )

        return

    await update.message.reply_text(
        "🔐 Для использования бота "
        "нужен пароль.\n\n"
        "Введите пароль:"
    )


# ============================================================
# /ID
# ============================================================

async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    await update.message.reply_text(
        f"Ваш Telegram ID:\n"
        f"`{update.effective_user.id}`",
        parse_mode="Markdown"
    )


# ============================================================
# /KEYS
# ============================================================

async def keys_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    user_id = update.effective_user.id

    if user_id != OWNER_ID:

        await update.message.reply_text(
            "⛔ У вас нет доступа."
        )

        return

    try:

        result = (
            supabase
            .table("bot_passwords")
            .select("*")
            .eq(
                "used",
                False
            )
            .order("id")
            .execute()
        )

        passwords = result.data or []

        if not passwords:

            await update.message.reply_text(
                "Свободных паролей нет."
            )

            return

        text = (
            "🔑 Свободные пароли:\n\n"
        )

        for index, row in enumerate(
            passwords,
            1
        ):

            text += (
                f"{index}. "
                f"`{row['password_text']}`\n"
            )

        await update.message.reply_text(
            text,
            parse_mode="Markdown"
        )

    except Exception as e:

        print(
            f"Ошибка /keys: {e}"
        )

        await update.message.reply_text(
            "❌ Не удалось получить "
            "список паролей."
        )


# ============================================================
# LATEX
# ============================================================

def latex_to_readable(
    text: str
) -> str:

    if not text:
        return text

    text = (
        text
        .replace("&gt;", ">")
        .replace("&lt;", "<")
        .replace("&ge;", "≥")
        .replace("&le;", "≤")
        .replace("&amp;", "&")
    )

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

    def replace_frac(match):

        numerator = match.group(1)
        denominator = match.group(2)

        return (
            f"({numerator}/{denominator})"
        )

    text = re.sub(
        r"\\(?:dfrac|tfrac|frac)\s*"
        r"\{([^{}]*)\}\s*"
        r"\{([^{}]*)\}",
        replace_frac,
        text
    )

    def replace_sqrt(match):

        content = match.group(1)

        return f"√({content})"

    text = re.sub(
        r"\\sqrt\s*\{([^{}]*)\}",
        replace_sqrt,
        text
    )

    superscript_map = str.maketrans(
        "0123456789+-=()nix",
        "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ"
    )

    def replace_power(match):

        content = match.group(1)

        if all(
            char in "0123456789+-=()nix"
            for char in content
        ):

            return content.translate(
                superscript_map
            )

        return (
            f"^({content})"
        )

    text = re.sub(
        r"\^\s*\{([^{}]*)\}",
        replace_power,
        text
    )

    replacements = {

        r"\mathbb{R}": "ℝ",
        r"\mathbb R": "ℝ",
        r"\R": "ℝ",

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

        r"\cup": "∪",
        r"\cap": "∩",

        r"\rightarrow": "→",
        r"\to": "→",

        r"\leftarrow": "←",

        r"\Rightarrow": "⇒",
        r"\Leftrightarrow": "⇔",

        r"\approx": "≈",
        r"\sim": "∼",
    }

    for old, new in replacements.items():

        text = text.replace(
            old,
            new
        )

    text = re.sub(
        r"\\(?:Bigg|bigg|Big|big|left|right|middle)\b",
        "",
        text
    )

    text = re.sub(
        r"\\[,;:!]\s*",
        "",
        text
    )

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

    text = re.sub(
        r"\\[a-zA-Z]+\b",
        "",
        text
    )

    text = text.replace(
        "{",
        ""
    )

    text = text.replace(
        "}",
        ""
    )

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


def clean_skysmart_text(
    value
) -> str:

    if value is None:
        return ""

    text = str(
        value
    )

    text = html.unescape(
        text
    )

    text = latex_to_readable(
        text
    )

    return text.strip()


# ============================================================
# SKYSMART URL
# ============================================================

def extract_room_name(
    text: str
):

    pattern = (
        r"https?://edu\.skysmart\.ru/"
        r"student/([^?\s]+)"
    )

    match = re.search(
        pattern,
        text,
        flags=re.IGNORECASE
    )

    if not match:
        return None

    room = match.group(1)

    room = room.rstrip(
        ".,!?)]}>"
    )

    return room


# ============================================================
# SKYSMART API
# ============================================================

def get_skysmart_answers(
    room_name: str
):

    response = requests.post(
        SKYSMART_API,
        json={
            "roomName": room_name
        },
        timeout=30
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# SKYSMART FORMAT
# ============================================================

def get_task_number(
    task,
    index
):

    if not isinstance(
        task,
        dict
    ):
        return index

    for key in (
        "number",
        "task_number",
        "taskNumber",
        "id",
    ):

        if key in task:

            value = task[key]

            if value is not None:
                return value

    return index


def get_task_question(
    task
):

    if not isinstance(
        task,
        dict
    ):
        return ""

    possible_keys = [
        "question",
        "question_text",
        "questionText",
        "text",
        "title",
        "condition",
        "task",
    ]

    for key in possible_keys:

        value = task.get(
            key
        )

        if value not in (
            None,
            ""
        ):

            return clean_skysmart_text(
                value
            )

    return ""


def get_task_answer(
    task
):

    if not isinstance(
        task,
        dict
    ):
        return ""

    possible_keys = [
        "answer",
        "answers",
        "correct_answer",
        "correctAnswer",
        "result",
        "solution",
    ]

    for key in possible_keys:

        value = task.get(
            key
        )

        if value not in (
            None,
            ""
        ):

            if isinstance(
                value,
                list
            ):

                return ", ".join(
                    clean_skysmart_text(x)
                    for x in value
                )

            if isinstance(
                value,
                dict
            ):

                return clean_skysmart_text(
                    value.get("text")
                    or value.get("value")
                    or value
                )

            return clean_skysmart_text(
                value
            )

    return ""


def format_skysmart(
    data
):

    if not isinstance(
        data,
        list
    ):

        return (
            "❌ Неожиданный ответ "
            "от сервера."
        )

    if not data:

        return (
            "❌ Ответ пустой."
        )

    tasks = data[0]

    if not isinstance(
        tasks,
        list
    ):

        return (
            "❌ Не удалось получить "
            "список заданий."
        )

    lines = []

    for index, task in enumerate(
        tasks,
        1
    ):

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

        if not question:
            question = "—"

        if not answer:
            answer = "—"

        lines.append(
            f"<b>Задание "
            f"{html.escape(str(number))}</b>\n"
            f"❓ {html.escape(question)}\n"
            f"✅ <b>Ответ:</b> "
            f"{html.escape(answer)}"
        )

    return "\n\n".join(
        lines
    )


# ============================================================
# РАЗБИВКА
# ============================================================

def split_message(
    text: str,
    max_length: int = MAX_MESSAGE_LENGTH
):

    if len(text) <= max_length:
        return [text]

    chunks = []
    current = ""

    blocks = text.split(
        "\n\n"
    )

    for block in blocks:

        if len(block) > max_length:

            if current:

                chunks.append(
                    current
                )

                current = ""

            for i in range(
                0,
                len(block),
                max_length
            ):

                chunks.append(
                    block[
                        i:i + max_length
                    ]
                )

            continue

        candidate = (
            block
            if not current
            else current
            + "\n\n"
            + block
        )

        if len(candidate) <= max_length:

            current = candidate

        else:

            if current:
                chunks.append(
                    current
                )

            current = block

    if current:
        chunks.append(
            current
        )

    return chunks


async def send_long_message(
    message,
    text: str
):

    chunks = split_message(
        text
    )

    for chunk in chunks:

        await message.reply_text(
            chunk,
            parse_mode="HTML",
            disable_web_page_preview=True
        )


# ============================================================
# GOOGLE FORMS
# ============================================================

async def handle_google_forms(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str
):

    form_url = extract_google_form_url(
        text
    )

    if not form_url:

        await update.message.reply_text(
            "❗ <b>Отправьте ссылку "
            "на Google Form.</b>\n\n"
            "Поддерживаются ссылки:\n"
            "• https://docs.google.com/forms/d/e/.../viewform\n"
            "• https://docs.google.com/forms/d/.../viewform\n"
            "• https://docs.google.com/forms/d/.../edit",
            parse_mode="HTML"
        )

        return

    processing_message = (
        await update.message.reply_text(
            "⏳ <b>Открываю Google Form...</b>",
            parse_mode="HTML"
        )
    )

    try:

        form_data = await asyncio.to_thread(
            get_google_form,
            form_url
        )

        result = format_google_form(
            form_data
        )

        await processing_message.delete()

        await send_long_message(
            update.message,
            result
        )

    except requests.Timeout:

        print(
            "Google Form timeout"
        )

        await processing_message.edit_text(
            "❌ <b>Google Form слишком долго "
            "отвечает.</b>\n\n"
            "Попробуйте ещё раз.",
            parse_mode="HTML"
        )

    except requests.HTTPError as e:

        print(
            f"Google Form HTTP error: {e}"
        )

        status = None

        if e.response is not None:

            status = e.response.status_code

        if status == 404:

            message = (
                "❌ <b>Google Form не найдена.</b>\n\n"
                "Проверьте ссылку."
            )

        elif status == 403:

            message = (
                "🔒 <b>Google не разрешил "
                "открыть эту форму.</b>\n\n"
                "Возможно, форма требует входа "
                "в Google-аккаунт или доступ "
                "ограничен."
            )

        else:

            message = (
                "❌ <b>Не удалось открыть "
                "Google Form.</b>\n\n"
                "Попробуйте ещё раз."
            )

        await processing_message.edit_text(
            message,
            parse_mode="HTML"
        )

    except RuntimeError as e:

        print(
            f"Google Form runtime error: {e}"
        )

        error_text = str(e)

        if (
            "FB_PUBLIC_LOAD_DATA_" in error_text
            or "структуру" in error_text
            or "разобрать" in error_text
        ):

            message = (
                "⚠️ <b>Не удалось разобрать "
                "эту Google Form.</b>\n\n"
                "Возможно, Google изменил формат "
                "страницы или форма требует "
                "авторизацию."
            )

        else:

            message = (
                "❌ <b>Ошибка Google Forms.</b>\n\n"
                f"{html.escape(error_text)}"
            )

        await processing_message.edit_text(
            message,
            parse_mode="HTML"
        )

    except requests.RequestException as e:

        print(
            f"Google Form request error: {e}"
        )

        await processing_message.edit_text(
            "❌ <b>Не удалось открыть "
            "Google Form.</b>\n\n"
            "Проверьте ссылку и попробуйте ещё раз.",
            parse_mode="HTML"
        )

    except Exception as e:

        print(
            f"Google Forms error: {e}"
        )

        await processing_message.edit_text(
            "❌ <b>Произошла ошибка при "
            "обработке Google Form.</b>\n\n"
            "Попробуйте ещё раз.",
            parse_mode="HTML"
        )


# ============================================================
# SKYSMART
# ============================================================

async def handle_skysmart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str
):

    room_name = extract_room_name(
        text
    )

    if not room_name:

        await update.message.reply_text(
            "❗ Отправьте ссылку "
            "на тест Skysmart.\n\n"
            "Пример:\n"
            "https://edu.skysmart.ru/student/..."
        )

        return

    processing_message = (
        await update.message.reply_text(
            "⏳ Получаю задания и ответы..."
        )
    )

    try:

        data = await asyncio.to_thread(
            get_skysmart_answers,
            room_name
        )

        result = format_skysmart(
            data
        )

        await processing_message.delete()

        await send_long_message(
            update.message,
            result
        )

    except requests.Timeout:

        await processing_message.edit_text(
            "❌ Сервер Skysmart "
            "слишком долго отвечает.\n"
            "Попробуйте ещё раз."
        )

    except requests.RequestException as e:

        print(
            f"Skysmart HTTP error: {e}"
        )

        await processing_message.edit_text(
            "❌ Не удалось получить "
            "ответы от Skysmart.\n"
            "Попробуйте ещё раз."
        )

    except Exception as e:

        print(
            f"Skysmart error: {e}"
        )

        await processing_message.edit_text(
            "❌ Произошла ошибка "
            "при обработке теста."
        )


# ============================================================
# ПОМОЩЬ
# ============================================================

async def show_help(
    update: Update
):

    await update.message.reply_text(
        "ℹ️ <b>Как пользоваться ботом</b>\n\n"

        "📚 <b>Skysmart</b>\n"
        "Выберите этот режим и отправьте "
        "ссылку на тест Skysmart.\n\n"

        "📄 <b>Google Forms</b>\n"
        "Выберите этот режим и отправьте "
        "ссылку на Google Form.\n\n"

        "Google Form может быть чужой — "
        "владельцем формы быть не обязательно, "
        "если форма доступна по публичной ссылке.\n\n"

        "🤖 Бот обработает ссылку "
        "в соответствии с выбранным режимом.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD
    )


# ============================================================
# ОБРАБОТКА СООБЩЕНИЙ
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    user_id = update.effective_user.id

    create_user_if_needed(
        user_id
    )

    text = update.message.text or ""

    text = text.strip()

    if not text:
        return

    # --------------------------------------------------------
    # НЕ АВТОРИЗОВАН
    # --------------------------------------------------------

    if not is_authorized(
        user_id
    ):

        if use_password(
            text,
            user_id
        ):

            await update.message.reply_text(
                "✅ Пароль принят!\n\n"
                "🤖 Авторизация успешна.\n\n"
                "Выберите режим:",
                reply_markup=MAIN_KEYBOARD
            )

        else:

            await update.message.reply_text(
                "❌ Неверный или уже "
                "использованный пароль."
            )

        return

    # --------------------------------------------------------
    # SKYSMART
    # --------------------------------------------------------

    if text == "📚 Skysmart":

        set_user_mode(
            user_id,
            "skysmart"
        )

        await update.message.reply_text(
            "📚 <b>Режим Skysmart выбран.</b>\n\n"
            "Теперь отправьте ссылку "
            "на тест Skysmart.",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )

        return

    # --------------------------------------------------------
    # GOOGLE FORMS
    # --------------------------------------------------------

    if text == "📄 Google Forms":

        set_user_mode(
            user_id,
            "google_forms"
        )

        await update.message.reply_text(
            "📄 <b>Режим Google Forms выбран.</b>\n\n"
            "Отправьте ссылку на Google Form.\n\n"
            "Поддерживается публичная ссылка:\n"
            "<code>/d/e/.../viewform</code>\n\n"
            "Владельцем формы быть не обязательно.",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )

        return

    # --------------------------------------------------------
    # ПОМОЩЬ
    # --------------------------------------------------------

    if text == "ℹ️ Помощь":

        await show_help(
            update
        )

        return

    # --------------------------------------------------------
    # ТЕКУЩИЙ РЕЖИМ
    # --------------------------------------------------------

    mode = get_user_mode(
        user_id
    )

    if mode == "google_forms":

        await handle_google_forms(
            update,
            context,
            text
        )

        return

    await handle_skysmart(
        update,
        context,
        text
    )


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

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
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        message_handler
    )
)


# ============================================================
# WEBHOOK
# ============================================================

WEBHOOK_PATH = "/telegram"


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
            f"Ошибка Telegram webhook: {e}"
        )

        return PlainTextResponse(
            "ERROR",
            status_code=500
        )


# ============================================================
# STARLETTE
# ============================================================

async def homepage(
    request: Request
):

    return PlainTextResponse(
        "Telegram bot is running."
    )


async def health(
    request: Request
):

    return PlainTextResponse(
        "OK"
    )


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def startup():

    print()
    print(
        "========================================"
    )
    print(
        "Запуск Telegram бота..."
    )
    print(
        "========================================"
    )

    try:

        initialize_passwords()

        await application.initialize()

        await application.start()

        webhook_url = (
            RENDER_URL
            + WEBHOOK_PATH
        )

        await application.bot.set_webhook(
            url=webhook_url
        )

        print(
            f"Webhook установлен:\n"
            f"{webhook_url}"
        )

        print(
            "Google Forms: HTML parser"
        )

        print(
            "Бот успешно запущен!"
        )

    except Exception as e:

        print(
            f"ОШИБКА ЗАПУСКА БОТА: {e}"
        )

        raise


async def shutdown():

    print(
        "Остановка Telegram бота..."
    )

    try:

        await application.stop()

        await application.shutdown()

    except Exception as e:

        print(
            f"Ошибка остановки: {e}"
        )


app = Starlette(
    routes=[],
    on_startup=[
        startup
    ],
    on_shutdown=[
        shutdown
    ]
)


# ============================================================
# ROUTES
# ============================================================

from starlette.routing import Route


app.router.routes.extend([

    Route(
        "/",
        homepage,
        methods=["GET", "HEAD"]
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

])


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )
