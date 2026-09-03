import os
import re
import html
import hashlib
import secrets
import asyncio
import json
import urllib.parse

import requests
import uvicorn

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, HTMLResponse

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from supabase import create_client

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

OWNER_ID = os.getenv("OWNER_ID")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    f"{RENDER_URL}/google/callback"
)

PORT = int(os.getenv("PORT", "10000"))

SKYSMART_API = "https://skysmart-answers.vercel.app/get_answers/"

MAX_MESSAGE_LENGTH = 3900


# ============================================================
# GOOGLE
# ============================================================

GOOGLE_FORMS_SCOPE = (
    "https://www.googleapis.com/auth/forms.body.readonly"
)

GOOGLE_TOKEN_ACCOUNT = "main"

# Временные OAuth state.
# Используются только во время первоначального подключения.
google_oauth_states = set()


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

if not GOOGLE_CLIENT_ID:
    raise RuntimeError("Не задан GOOGLE_CLIENT_ID")

if not GOOGLE_CLIENT_SECRET:
    raise RuntimeError("Не задан GOOGLE_CLIENT_SECRET")

if not GOOGLE_REDIRECT_URI:
    raise RuntimeError("Не задан GOOGLE_REDIRECT_URI")


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
        ["📚 Skysmart", "📄 Google Forms"],
        ["ℹ️ Помощь"],
    ],
    resize_keyboard=True,
)


# ============================================================
# РЕЖИМЫ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================

user_modes = {}


def get_user_mode(user_id: int) -> str:
    return user_modes.get(user_id, "skysmart")


def set_user_mode(user_id: int, mode: str):
    user_modes[user_id] = mode


# ============================================================
# ПАРОЛИ
# ============================================================

def hash_password(password: str) -> str:

    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def generate_password() -> str:

    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

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

        current_count = len(result.data or [])

        while current_count < 10:

            password = generate_password()

            password_hash = hash_password(password)

            existing = (
                supabase
                .table("bot_passwords")
                .select("id")
                .eq("password_hash", password_hash)
                .execute()
            )

            if existing.data:
                continue

            supabase.table("bot_passwords").insert({
                "password_hash": password_hash,
                "password_text": password,
                "used": False,
                "used_by": None,
                "used_at": None,
            }).execute()

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
            .eq("telegram_id", user_id)
            .limit(1)
            .execute()
        )

        if not result.data:
            return False

        return bool(
            result.data[0].get("authorized")
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

        print(
            f"Ошибка создания пользователя: {e}"
        )


def use_password(
    password: str,
    user_id: int
) -> bool:

    password = password.strip().upper()

    password_hash = hash_password(password)

    try:

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
        }).eq(
            "id",
            password_row["id"]
        ).execute()

        supabase.table("bot_users").update({
            "authorized": True,
        }).eq(
            "telegram_id",
            user_id
        ).execute()

        return True

    except Exception as e:

        print(
            f"Ошибка использования пароля: {e}"
        )

        return False


# ============================================================
# GOOGLE OAUTH
# ============================================================

def get_google_client_config():

    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [
                GOOGLE_REDIRECT_URI
            ],
        }
    }


def create_google_flow(
    state=None
):

    flow = Flow.from_client_config(
        get_google_client_config(),
        scopes=[GOOGLE_FORMS_SCOPE],
        redirect_uri=GOOGLE_REDIRECT_URI,
    )

    if state:
        flow.state = state

    return flow


def get_saved_google_credentials():

    try:

        result = (
            supabase
            .table("google_tokens")
            .select("token_json")
            .eq(
                "account_name",
                GOOGLE_TOKEN_ACCOUNT
            )
            .limit(1)
            .execute()
        )

        if not result.data:
            return None

        token_json = result.data[0].get(
            "token_json"
        )

        if not token_json:
            return None

        credentials = Credentials.from_authorized_user_info(
            json.loads(token_json),
            scopes=[GOOGLE_FORMS_SCOPE]
        )

        return credentials

    except Exception as e:

        print(
            f"Ошибка получения Google token: {e}"
        )

        return None


def save_google_credentials(
    credentials
):

    token_json = credentials.to_json()

    try:

        existing = (
            supabase
            .table("google_tokens")
            .select("id")
            .eq(
                "account_name",
                GOOGLE_TOKEN_ACCOUNT
            )
            .limit(1)
            .execute()
        )

        payload = {
            "account_name": GOOGLE_TOKEN_ACCOUNT,
            "token_json": token_json,
            "updated_at": "now()",
        }

        if existing.data:

            supabase.table(
                "google_tokens"
            ).update(
                payload
            ).eq(
                "account_name",
                GOOGLE_TOKEN_ACCOUNT
            ).execute()

        else:

            supabase.table(
                "google_tokens"
            ).insert(
                payload
            ).execute()

        print(
            "Google OAuth token сохранён."
        )

    except Exception as e:

        print(
            f"Ошибка сохранения Google token: {e}"
        )

        raise


def get_google_service():

    credentials = get_saved_google_credentials()

    if not credentials:
        return None

    # Если access token истёк,
    # google-auth автоматически использует
    # refresh token при обращении к API.
    if credentials.expired and credentials.refresh_token:

        try:

            from google.auth.transport.requests import Request as GoogleRequest

            credentials.refresh(
                GoogleRequest()
            )

            save_google_credentials(
                credentials
            )

        except Exception as e:

            print(
                f"Ошибка обновления Google token: {e}"
            )

            return None

    try:

        service = build(
            "forms",
            "v1",
            credentials=credentials,
            cache_discovery=False
        )

        return service

    except Exception as e:

        print(
            f"Ошибка создания Google Forms service: {e}"
        )

        return None


def extract_google_form_id(text: str):

    patterns = [

        # https://docs.google.com/forms/d/FORM_ID/edit
        r"docs\.google\.com/forms/d/([a-zA-Z0-9_-]+)",

        # https://docs.google.com/forms/d/e/FORM_ID/viewform
        r"docs\.google\.com/forms/d/e/([a-zA-Z0-9_-]+)",

    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:

            return match.group(1)

    return None


def get_google_form(
    form_id: str
):

    service = get_google_service()

    if not service:

        raise RuntimeError(
            "Google аккаунт ещё не подключён."
        )

    return service.forms().get(
        formId=form_id
    ).execute()


def format_google_form(
    form_data
):

    if not isinstance(
        form_data,
        dict
    ):
        return (
            "❌ Google Forms вернул "
            "неожиданный ответ."
        )

    info = form_data.get(
        "info",
        {}
    )

    title = info.get(
        "title",
        "Google Form"
    )

    description = info.get(
        "description",
        ""
    )

    items = form_data.get(
        "items",
        []
    )

    lines = []

    lines.append(
        "📄 <b>Google Forms</b>"
    )

    lines.append(
        f"📝 <b>{html.escape(str(title))}</b>"
    )

    if description:

        clean_description = re.sub(
            r"\s+",
            " ",
            str(description)
        ).strip()

        if clean_description:

            lines.append(
                f"ℹ️ {html.escape(clean_description)}"
            )

    lines.append("")

    question_number = 0

    for item in items:

        if not isinstance(
            item,
            dict
        ):
            continue

        title_text = item.get(
            "title",
            ""
        )

        question_item = item.get(
            "questionItem"
        )

        # Не вопрос — например,
        # заголовок/описание раздела.
        if not question_item:

            continue

        question_number += 1

        lines.append(
            f"<b>{question_number}. "
            f"{html.escape(str(title_text))}</b>"
        )

        question = question_item.get(
            "question",
            {}
        )

        question_type = question.get(
            "choiceQuestion"
        )

        if question_type:

            choices = question_type.get(
                "options",
                []
            )

            for choice in choices:

                if not isinstance(
                    choice,
                    dict
                ):
                    continue

                value = choice.get(
                    "value",
                    ""
                )

                if value:

                    lines.append(
                        f"   • "
                        f"{html.escape(str(value))}"
                    )

        text_question = question.get(
            "textQuestion"
        )

        if text_question:

            lines.append(
                "   ✏️ <i>Поле для ввода ответа</i>"
            )

        scale_question = question.get(
            "scaleQuestion"
        )

        if scale_question:

            low = scale_question.get(
                "low",
                ""
            )

            high = scale_question.get(
                "high",
                ""
            )

            low_label = scale_question.get(
                "lowLabel",
                ""
            )

            high_label = scale_question.get(
                "highLabel",
                ""
            )

            scale_text = (
                f"   📊 Шкала: "
                f"{low}–{high}"
            )

            if low_label:

                scale_text += (
                    f" ({low_label}"
                )

            if high_label:

                if low_label:
                    scale_text += (
                        f" → {high_label})"
                    )
                else:
                    scale_text += (
                        f" ({high_label})"
                    )

            elif low_label:

                scale_text += ")"

            lines.append(
                scale_text
            )

        lines.append("")

    if question_number == 0:

        return (
            "❌ В форме не найдено вопросов.\n\n"
            "Возможно, форма содержит только "
            "разделы или описание."
        )

    return "\n".join(
        lines
    ).strip()


# ============================================================
# /GOOGLE
# ============================================================

async def google_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    user_id = update.effective_user.id

    if not is_authorized(user_id):

        await update.message.reply_text(
            "⛔ Сначала авторизуйтесь в боте."
        )

        return

    state = secrets.token_urlsafe(32)

    google_oauth_states.add(state)

    flow = create_google_flow(
        state=state
    )

    authorization_url, returned_state = (
        flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true"
        )
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔐 Подключить Google",
                url=authorization_url
            )
        ]
    ])

    await update.message.reply_text(
        "🔐 <b>Подключение Google-аккаунта</b>\n\n"
        "Нажмите кнопку ниже и войдите "
        "именно в созданный аккаунт-пустышку.\n\n"
        "После подтверждения Google вернёт "
        "вас на наш сервер.",
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ============================================================
# GOOGLE CALLBACK
# ============================================================

async def google_callback(
    request: Request
):

    error = request.query_params.get(
        "error"
    )

    if error:

        return HTMLResponse(
            f"""
            <html>
            <body>
            <h2>❌ Google авторизация отменена</h2>
            <p>{html.escape(error)}</p>
            <p>Можно закрыть эту страницу.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    state = request.query_params.get(
        "state"
    )

    code = request.query_params.get(
        "code"
    )

    if not state or not code:

        return HTMLResponse(
            """
            <html>
            <body>
            <h2>❌ Неверный OAuth callback</h2>
            <p>Не хватает параметров state или code.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    if state not in google_oauth_states:

        return HTMLResponse(
            """
            <html>
            <body>
            <h2>❌ OAuth-сессия недействительна</h2>
            <p>Попробуйте запустить /google ещё раз.</p>
            </body>
            </html>
            """,
            status_code=400
        )

    google_oauth_states.discard(
        state
    )

    try:

        flow = create_google_flow(
            state=state
        )

        flow.fetch_token(
            code=code
        )

        credentials = flow.credentials

        if not credentials.refresh_token:

            return HTMLResponse(
                """
                <html>
                <body>
                <h2>❌ Google не выдал refresh token</h2>
                <p>
                Попробуйте снова и разрешите доступ.
                </p>
                </body>
                </html>
                """,
                status_code=400
            )

        save_google_credentials(
            credentials
        )

        return HTMLResponse(
            """
            <html>
            <head>
                <meta charset="utf-8">
                <title>Google подключён</title>
            </head>
            <body>
                <h2>✅ Google успешно подключён!</h2>
                <p>
                Аккаунт-пустышка авторизован.
                </p>
                <p>
                Теперь можно вернуться в Telegram
                и отправить ссылку на Google Form.
                </p>
            </body>
            </html>
            """
        )

    except Exception as e:

        print(
            f"Google OAuth callback error: {e}"
        )

        return HTMLResponse(
            """
            <html>
            <head>
                <meta charset="utf-8">
            </head>
            <body>
                <h2>❌ Ошибка авторизации Google</h2>
                <p>
                Не удалось сохранить авторизацию.
                </p>
                <p>
                Проверьте настройки Google Cloud
                и попробуйте ещё раз.
                </p>
            </body>
            </html>
            """,
            status_code=500
        )


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

    create_user_if_needed(user_id)

    if is_authorized(user_id):

        await update.message.reply_text(
            "🤖 <b>Добро пожаловать!</b>\n\n"
            "Выберите режим на клавиатуре ниже:",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )

        return

    await update.message.reply_text(
        "🔐 Для использования бота нужен пароль.\n\n"
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
            .eq("used", False)
            .order("id")
            .execute()
        )

        passwords = result.data or []

        if not passwords:

            await update.message.reply_text(
                "Свободных паролей нет."
            )

            return

        text = "🔑 Свободные пароли:\n\n"

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
            "❌ Не удалось получить список паролей."
        )


# ============================================================
# LATEX → ЧИТАЕМЫЙ ВИД
# ============================================================

def latex_to_readable(text: str) -> str:

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

        return f"({numerator}/{denominator})"

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

        return f"^({content})"

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
        text = text.replace(old, new)

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

    text = text.replace("{", "")
    text = text.replace("}", "")

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


def clean_skysmart_text(value) -> str:

    if value is None:
        return ""

    text = str(value)

    text = html.unescape(text)

    text = latex_to_readable(text)

    return text.strip()


# ============================================================
# SKYSMART URL
# ============================================================

def extract_room_name(text: str):

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
# ФОРМАТИРОВАНИЕ SKYSMART
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

        value = task.get(key)

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

        value = task.get(key)

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
        return "❌ Ответ пустой."

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
# РАЗБИВКА ДЛИННОГО СООБЩЕНИЯ
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
# ОБРАБОТКА GOOGLE FORMS
# ============================================================

async def handle_google_forms(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str
):

    form_id = extract_google_form_id(
        text
    )

    if not form_id:

        await update.message.reply_text(
            "❗ <b>Отправьте ссылку "
            "на Google Form.</b>\n\n"
            "Например:\n"
            "https://docs.google.com/forms/d/...",
            parse_mode="HTML"
        )

        return

    processing_message = (
        await update.message.reply_text(
            "⏳ <b>Получаю Google Form...</b>",
            parse_mode="HTML"
        )
    )

    try:

        form_data = await asyncio.to_thread(
            get_google_form,
            form_id
        )

        result = format_google_form(
            form_data
        )

        await processing_message.delete()

        await send_long_message(
            update.message,
            result
        )

    except HttpError as e:

        print(
            f"Google Forms API error: {e}"
        )

        status = getattr(
            e.resp,
            "status",
            None
        )

        if status == 403:

            message = (
                "❌ Google не разрешил "
                "доступ к этой форме.\n\n"
                "Проверьте, что аккаунт-пустышка "
                "имеет доступ к форме."
            )

        elif status == 404:

            message = (
                "❌ Форма не найдена.\n\n"
                "Проверьте ссылку."
            )

        else:

            message = (
                "❌ Google Forms API вернул ошибку.\n"
                "Попробуйте ещё раз."
            )

        await processing_message.edit_text(
            message
        )

    except RuntimeError as e:

        print(
            f"Google Forms runtime error: {e}"
        )

        await processing_message.edit_text(
            "🔐 <b>Google ещё не подключён.</b>\n\n"
            "Используйте команду /google, "
            "чтобы подключить аккаунт-пустышку.",
            parse_mode="HTML"
        )

    except requests.RequestException as e:

        print(
            f"Google Forms request error: {e}"
        )

        await processing_message.edit_text(
            "❌ Не удалось связаться с Google."
        )

    except Exception as e:

        print(
            f"Google Forms error: {e}"
        )

        await processing_message.edit_text(
            "❌ Произошла ошибка при "
            "обработке Google Form."
        )


# ============================================================
# ОБРАБОТКА SKYSMART
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
            "https://edu.skysmart.ru/student/...",
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

        "🔐 <b>Google</b>\n"
        "Команда /google используется для "
        "первоначального подключения "
        "аккаунта-пустышки.\n\n"

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
            "Если форма требует входа, "
            "сначала выполните /google.",
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

    # --------------------------------------------------------
    # GOOGLE FORMS
    # --------------------------------------------------------

    if mode == "google_forms":

        await handle_google_forms(
            update,
            context,
            text
        )

        return

    # --------------------------------------------------------
    # SKYSMART
    # --------------------------------------------------------

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
    CommandHandler(
        "google",
        google_command
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
# GOOGLE CALLBACK ROUTE
# ============================================================

async def google_callback_route(
    request: Request
):

    return await google_callback(
        request
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
            f"Google callback:\n"
            f"{GOOGLE_REDIRECT_URI}"
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

    Route(
        "/google/callback",
        google_callback_route,
        methods=["GET"]
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
