import os
import re
import html
import hashlib
import secrets
import asyncio
import json
import base64

from datetime import datetime, timezone

import requests
import uvicorn

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, HTMLResponse
from starlette.routing import Route

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

from openai import OpenAI


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

RENDER_URL = os.getenv(
    "RENDER_URL",
    ""
).rstrip("/")

SUPABASE_URL = os.getenv(
    "SUPABASE_URL"
)

SUPABASE_SERVICE_KEY = os.getenv(
    "SUPABASE_SERVICE_KEY"
)

OWNER_ID = os.getenv(
    "OWNER_ID"
)

GOOGLE_CLIENT_ID = os.getenv(
    "GOOGLE_CLIENT_ID"
)

GOOGLE_CLIENT_SECRET = os.getenv(
    "GOOGLE_CLIENT_SECRET"
)

OPENAI_API_KEY = os.getenv(
    "OPENAI_API_KEY"
)

GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    f"{RENDER_URL}/google/callback"
)

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
# GOOGLE SCOPES
# ============================================================

GOOGLE_FORMS_SCOPE = (
    "https://www.googleapis.com/auth/forms.body.readonly"
)

GOOGLE_DRIVE_SCOPE = (
    "https://www.googleapis.com/auth/drive.readonly"
)

GOOGLE_SCOPES = [

    GOOGLE_FORMS_SCOPE,

    GOOGLE_DRIVE_SCOPE,

]

GOOGLE_TOKEN_ACCOUNT = "main"

GOOGLE_OAUTH_STATE_TTL = (
    15 * 60
)


# ============================================================
# ПРОВЕРКА НАСТРОЕК
# ============================================================

if not BOT_TOKEN:

    raise RuntimeError(
        "Не задан BOT_TOKEN"
    )


if not RENDER_URL:

    raise RuntimeError(
        "Не задан RENDER_URL"
    )


if not SUPABASE_URL:

    raise RuntimeError(
        "Не задан SUPABASE_URL"
    )


if not SUPABASE_SERVICE_KEY:

    raise RuntimeError(
        "Не задан SUPABASE_SERVICE_KEY"
    )


if not OWNER_ID:

    raise RuntimeError(
        "Не задан OWNER_ID"
    )


if not GOOGLE_CLIENT_ID:

    raise RuntimeError(
        "Не задан GOOGLE_CLIENT_ID"
    )


if not GOOGLE_CLIENT_SECRET:

    raise RuntimeError(
        "Не задан GOOGLE_CLIENT_SECRET"
    )


if not OPENAI_API_KEY:

    raise RuntimeError(
        "Не задан OPENAI_API_KEY"
    )


OWNER_ID = int(
    OWNER_ID
)


# ============================================================
# SUPABASE
# ============================================================

supabase = create_client(

    SUPABASE_URL,

    SUPABASE_SERVICE_KEY

)


# ============================================================
# OPENAI
# ============================================================

openai_client = OpenAI(

    api_key=OPENAI_API_KEY

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

    resize_keyboard=True

)


# ============================================================
# РЕЖИМЫ
# ============================================================

user_modes = {}


def get_user_mode(
    user_id: int
) -> str:

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

def hash_password(
    password: str
) -> str:

    return hashlib.sha256(

        password.encode(
            "utf-8"
        )

    ).hexdigest()


def generate_password() -> str:

    alphabet = (

        "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

    )

    first = "".join(

        secrets.choice(
            alphabet
        )

        for _ in range(4)

    )

    second = "".join(

        secrets.choice(
            alphabet
        )

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

def is_authorized(
    user_id: int
) -> bool:

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


def create_user_if_needed(
    user_id: int
):

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
# GOOGLE OAUTH CONFIG
# ============================================================

def get_google_client_config():

    return {

        "web": {

            "client_id": GOOGLE_CLIENT_ID,

            "client_secret": GOOGLE_CLIENT_SECRET,

            "auth_uri": (
                "https://accounts.google.com/o/oauth2/auth"
            ),

            "token_uri": (
                "https://oauth2.googleapis.com/token"
            ),

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

        scopes=GOOGLE_SCOPES,

        redirect_uri=GOOGLE_REDIRECT_URI,

    )

    if state:

        flow.state = state

    return flow


# ============================================================
# GOOGLE OAUTH STATE
# ============================================================

def save_google_oauth_state(

    state: str,

    user_id: int

):

    try:

        cleanup_google_oauth_states()

        (

            supabase

            .table("google_oauth_states")

            .insert({

                "state": state,

                "user_id": user_id,

                "created_at": datetime.now(

                    timezone.utc

                ).isoformat(),

            })

            .execute()

        )

        return True

    except Exception as e:

        print(

            f"Ошибка сохранения OAuth state: {e}"

        )

        return False


def get_google_oauth_state(
    state: str
):

    try:

        result = (

            supabase

            .table("google_oauth_states")

            .select("*")

            .eq(

                "state",

                state

            )

            .limit(1)

            .execute()

        )

        if not result.data:

            return None

        row = result.data[0]

        created_at = row.get(

            "created_at"

        )

        if created_at:

            created_dt = datetime.fromisoformat(

                str(created_at).replace(

                    "Z",

                    "+00:00"

                )

            )

            if created_dt.tzinfo is None:

                created_dt = created_dt.replace(

                    tzinfo=timezone.utc

                )

            age = (

                datetime.now(timezone.utc)

                - created_dt

            ).total_seconds()

            if age > GOOGLE_OAUTH_STATE_TTL:

                delete_google_oauth_state(

                    state

                )

                return None

        return row

    except Exception as e:

        print(

            f"Ошибка получения OAuth state: {e}"

        )

        return None


def delete_google_oauth_state(
    state: str
):

    try:

        (

            supabase

            .table("google_oauth_states")

            .delete()

            .eq(

                "state",

                state

            )

            .execute()

        )

    except Exception as e:

        print(

            f"Ошибка удаления OAuth state: {e}"

        )


def cleanup_google_oauth_states():

    try:

        result = (

            supabase

            .table("google_oauth_states")

            .select(

                "state,created_at"

            )

            .execute()

        )

        if not result.data:

            return

        now = datetime.now(

            timezone.utc

        )

        for row in result.data:

            created_at = row.get(

                "created_at"

            )

            if not created_at:

                continue

            try:

                created_dt = datetime.fromisoformat(

                    str(created_at).replace(

                        "Z",

                        "+00:00"

                    )

                )

                if created_dt.tzinfo is None:

                    created_dt = created_dt.replace(

                        tzinfo=timezone.utc

                    )

                age = (

                    now - created_dt

                ).total_seconds()

                if age > GOOGLE_OAUTH_STATE_TTL:

                    delete_google_oauth_state(

                        row["state"]

                    )

            except Exception:

                continue

    except Exception as e:

        print(

            f"Ошибка очистки OAuth states: {e}"

        )


# ============================================================
# GOOGLE TOKEN
# ============================================================

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

        token_json = (

            result.data[0].get(

                "token_json"

            )

        )

        if not token_json:

            return None

        if isinstance(
            token_json,
            dict
        ):

            token_data = token_json

        else:

            token_data = json.loads(

                token_json

            )

        credentials = (

            Credentials

            .from_authorized_user_info(

                token_data,

                scopes=GOOGLE_SCOPES

            )

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

            "account_name":

                GOOGLE_TOKEN_ACCOUNT,

            "token_json":

                token_json,

        }

        if existing.data:

            (

                supabase

                .table("google_tokens")

                .update(payload)

                .eq(

                    "account_name",

                    GOOGLE_TOKEN_ACCOUNT

                )

                .execute()

            )

        else:

            (

                supabase

                .table("google_tokens")

                .insert(payload)

                .execute()

            )

    except Exception as e:

        print(

            f"Ошибка сохранения Google token: {e}"

        )

        raise


def refresh_google_credentials(
    credentials
):

    if (

        credentials

        and credentials.refresh_token

        and not credentials.valid

    ):

        try:

            from google.auth.transport.requests import (

                Request as GoogleRequest

            )

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

    if not credentials or not credentials.valid:

        return None

    return credentials


def get_google_service():

    credentials = (

        get_saved_google_credentials()

    )

    credentials = refresh_google_credentials(

        credentials

    )

    if not credentials:

        return None

    return build(

        "forms",

        "v1",

        credentials=credentials,

        cache_discovery=False

    )


def get_google_drive_service():

    credentials = (

        get_saved_google_credentials()

    )

    credentials = refresh_google_credentials(

        credentials

    )

    if not credentials:

        return None

    return build(

        "drive",

        "v3",

        credentials=credentials,

        cache_discovery=False

    )


# ============================================================
# GOOGLE FORM URL
# ============================================================

def extract_google_form_id(
    text: str
):

    if not text:

        return None

    match = re.search(

        r"https?://docs\.google\.com/forms/d/e/"
        r"([a-zA-Z0-9_-]+)"
        r"(?:/[^?\s]*)?",

        text,

        flags=re.IGNORECASE

    )

    if match:

        return {

            "type": "published",

            "id": match.group(1),

            "url": match.group(0)

        }

    match = re.search(

        r"https?://docs\.google\.com/forms/d/"
        r"(?!e(?:/|$))"
        r"([a-zA-Z0-9_-]+)"
        r"(?:/[^?\s]*)?",

        text,

        flags=re.IGNORECASE

    )

    if match:

        return {

            "type": "direct",

            "id": match.group(1),

            "url": match.group(0)

        }

    return None


# ============================================================
# RESOLVE PUBLISHED FORM
# ============================================================

def resolve_published_google_form_id(
    published_url: str
):

    drive_service = (

        get_google_drive_service()

    )

    forms_service = (

        get_google_service()

    )

    if not drive_service or not forms_service:

        raise RuntimeError(

            "Google аккаунт ещё не подключён."

        )

    match = re.search(

        r"/forms/d/e/"
        r"([a-zA-Z0-9_-]+)"
        r"/",

        published_url,

        flags=re.IGNORECASE

    )

    if not match:

        raise RuntimeError(

            "Некорректная публичная ссылка."

        )

    public_id = match.group(1)

    response = (

        drive_service

        .files()

        .list(

            q=(

                "mimeType = "
                "'application/vnd.google-apps.form' "
                "and trashed = false"

            ),

            spaces="drive",

            fields=(

                "files(id,name,webViewLink)"

            ),

            pageSize=1000

        )

        .execute()

    )

    files = response.get(

        "files",

        []

    )

    for file in files:

        form_id = file.get(

            "id"

        )

        if not form_id:

            continue

        try:

            form_data = (

                forms_service

                .forms()

                .get(

                    formId=form_id

                )

                .execute()

            )

        except HttpError:

            continue

        responder_uri = (

            form_data.get(

                "responderUri"

            )

            or ""

        )

        if public_id in responder_uri:

            return form_id

        if (

            responder_uri

            and

            published_url.rstrip("/")

            ==

            responder_uri.rstrip("/")

        ):

            return form_id

    return None


# ============================================================
# GET GOOGLE FORM
# ============================================================

def get_google_form(
    form_reference
):

    if isinstance(

        form_reference,

        dict

    ):

        form_type = (

            form_reference.get(

                "type"

            )

        )

        form_id = (

            form_reference.get(

                "id"

            )

        )

        if form_type == "published":

            form_id = (

                resolve_published_google_form_id(

                    form_reference.get(

                        "url"

                    )

                )

            )

            if not form_id:

                raise RuntimeError(

                    "Не удалось найти опубликованную форму."

                )

    else:

        form_id = form_reference

    if not form_id:

        raise RuntimeError(

            "Не указан ID Google Form."

        )

    service = get_google_service()

    if not service:

        raise RuntimeError(

            "Google аккаунт ещё не подключён."

        )

    return (

        service

        .forms()

        .get(

            formId=form_id

        )

        .execute()

    )


# ============================================================
# GOOGLE FORM IMAGES
# ============================================================

def extract_google_form_images(
    form_data
):

    images = []

    items = form_data.get(

        "items",

        []

    )

    for index, item in enumerate(

        items,

        1

    ):

        if not isinstance(

            item,

            dict

        ):

            continue

        item_title = (

            item.get("title")

            or

            f"Задание {index}"

        )

        # ----------------------------------------------------
        # IMAGE ITEM
        # ----------------------------------------------------

        image_item = item.get(

            "imageItem"

        )

        if image_item:

            image = image_item.get(

                "image",

                {}

            )

            content_uri = image.get(

                "contentUri"

            )

            if content_uri:

                images.append({

                    "number": index,

                    "title": item_title,

                    "url": content_uri,

                    "type": "imageItem",

                })

        # ----------------------------------------------------
        # QUESTION ITEM IMAGE
        # ----------------------------------------------------

        question_item = item.get(

            "questionItem"

        )

        if question_item:

            image = question_item.get(

                "image",

                {}

            )

            content_uri = image.get(

                "contentUri"

            )

            if content_uri:

                images.append({

                    "number": index,

                    "title": item_title,

                    "url": content_uri,

                    "type": "questionItem",

                })

        # ----------------------------------------------------
        # QUESTION GROUP IMAGE
        # ----------------------------------------------------

        question_group = item.get(

            "questionGroupItem"

        )

        if question_group:

            image = question_group.get(

                "image",

                {}

            )

            content_uri = image.get(

                "contentUri"

            )

            if content_uri:

                images.append({

                    "number": index,

                    "title": item_title,

                    "url": content_uri,

                    "type": "questionGroupItem",

                })

    return images


# ============================================================
# DOWNLOAD GOOGLE IMAGE
# ============================================================

def download_google_image(
    image_url: str
):

    response = requests.get(

        image_url,

        timeout=30

    )

    response.raise_for_status()

    return (

        response.content,

        response.headers.get(

            "Content-Type",

            "image/jpeg"

        )

    )


# ============================================================
# ANALYZE IMAGE
# ============================================================

def analyze_task_image(

    image_bytes: bytes,

    content_type: str

):

    # --------------------------------------------------------
    # Определяем MIME
    # --------------------------------------------------------

    if not content_type.startswith(

        "image/"

    ):

        content_type = "image/jpeg"

    # --------------------------------------------------------
    # BASE64
    # --------------------------------------------------------

    image_base64 = base64.b64encode(

        image_bytes

    ).decode(

        "utf-8"

    )

    image_url = (

        f"data:{content_type};base64,"

        f"{image_base64}"

    )

    # --------------------------------------------------------
    # PROMPT
    # --------------------------------------------------------

    prompt = """

На изображении находится учебное задание.

Внимательно изучи изображение.

Сначала перепиши условие задания.

Затем объясни, как его решать.

Если изображение содержит математическую
задачу, выполни необходимые вычисления
и объясни ход решения.

Не выдумывай текст.

Если часть изображения плохо читается,
прямо напиши об этом.

Используй формат:

📖 Задание:
...

🧠 Разбор:
...

📝 Решение:
...

🎯 Итог:
...

"""

    # --------------------------------------------------------
    # OPENAI
    # --------------------------------------------------------

    response = (

        openai_client.responses.create(

            model="gpt-5.6-luna",

            input=[

                {

                    "role": "user",

                    "content": [

                        {

                            "type": "input_text",

                            "text": prompt

                        },

                        {

                            "type": "input_image",

                            "image_url": image_url,

                        }

                    ]

                }

            ]

        )

    )

    return (

        response.output_text

    )


# ============================================================
# GOOGLE FORM TEXT FORMAT
# ============================================================

def format_google_form(
    form_data
):

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

        lines.append(

            html.escape(

                str(description)

            )

        )

    lines.append("")

    question_number = 0

    for item in items:

        question_item = item.get(

            "questionItem"

        )

        if not question_item:

            continue

        question_number += 1

        title_text = item.get(

            "title",

            ""

        )

        lines.append(

            f"<b>{question_number}. "

            f"{html.escape(str(title_text))}"

            f"</b>"

        )

        question = question_item.get(

            "question",

            {}

        )

        choice_question = question.get(

            "choiceQuestion"

        )

        if choice_question:

            choices = choice_question.get(

                "options",

                []

            )

            for choice in choices:

                value = choice.get(

                    "value",

                    ""

                )

                if value:

                    lines.append(

                        "• "

                        + html.escape(

                            str(value)

                        )

                    )

        text_question = question.get(

            "textQuestion"

        )

        if text_question:

            lines.append(

                "✏️ "

                "<i>Поле для ответа</i>"

            )

        lines.append("")

    return "\n".join(

        lines

    )


# ============================================================
# FORMAT RESULTS
# ============================================================

def format_google_image_result(

    image,

    result

):

    number = image.get(

        "number",

        "?"

    )

    title = image.get(

        "title",

        ""

    )

    lines = []

    lines.append(

        f"📷 <b>Задание {number}</b>"

    )

    if title:

        lines.append(

            f"📝 {html.escape(str(title))}"

        )

    lines.append("")

    # Не экранируем результат:
    # иначе эмодзи и текст будут выглядеть нормально,
    # но HTML-теги от модели не используются.

    lines.append(

        html.escape(

            str(result)

        )

    )

    return "\n".join(

        lines

    )


# ============================================================
# SKYSMART
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

    return match.group(1).rstrip(

        ".,!?)]}>"

    )


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


def clean_skysmart_text(
    value
):

    if value is None:

        return ""

    return html.unescape(

        str(value)

    ).strip()


def get_task_question(
    task
):

    for key in [

        "question",

        "question_text",

        "questionText",

        "text",

        "title",

        "condition",

        "task",

    ]:

        value = task.get(key)

        if value:

            return clean_skysmart_text(

                value

            )

    return ""


def get_task_answer(
    task
):

    for key in [

        "answer",

        "answers",

        "correct_answer",

        "correctAnswer",

        "result",

        "solution",

    ]:

        value = task.get(key)

        if value:

            if isinstance(

                value,

                list

            ):

                return ", ".join(

                    clean_skysmart_text(x)

                    for x in value

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

            "❌ Неожиданный ответ сервера."

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

            "❌ Не удалось получить задания."

        )

    lines = []

    for index, task in enumerate(

        tasks,

        1

    ):

        question = get_task_question(

            task

        )

        answer = get_task_answer(

            task

        )

        lines.append(

            f"<b>Задание {index}</b>\n"

            f"❓ {html.escape(question)}\n"

            f"📝 {html.escape(answer)}"

        )

    return "\n\n".join(

        lines

    )


# ============================================================
# РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ
# ============================================================

def split_message(

    text: str,

    max_length: int = MAX_MESSAGE_LENGTH

):

    if len(text) <= max_length:

        return [

            text

        ]

    chunks = []

    current = ""

    blocks = text.split(

        "\n\n"

    )

    for block in blocks:

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
# GOOGLE FORMS HANDLER
# ============================================================

async def handle_google_forms(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE,

    text: str

):

    form_reference = extract_google_form_id(

        text

    )

    if not form_reference:

        await update.message.reply_text(

            "❗ <b>Отправьте ссылку на Google Form.</b>\n\n"

            "Поддерживаются ссылки:\n"

            "• /d/FORM_ID/edit\n"

            "• /d/FORM_ID/viewform\n"

            "• /d/e/.../viewform",

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

        # ----------------------------------------------------
        # Получаем форму
        # ----------------------------------------------------

        form_data = await asyncio.to_thread(

            get_google_form,

            form_reference

        )

        # ----------------------------------------------------
        # Ищем изображения
        # ----------------------------------------------------

        images = extract_google_form_images(

            form_data

        )

        print(

            f"Найдено изображений: {len(images)}"

        )

        # ----------------------------------------------------
        # Если изображений нет
        # ----------------------------------------------------

        if not images:

            await processing_message.delete()

            result = format_google_form(

                form_data

            )

            await send_long_message(

                update.message,

                result

            )

            return

        total = len(images)

        await processing_message.edit_text(

            "🖼️ <b>Найдены изображения!</b>\n\n"

            f"Количество: <b>{total}</b>\n\n"

            "🔍 Начинаю читать задания...",

            parse_mode="HTML"

        )

        # ----------------------------------------------------
        # ОБРАБОТКА КАЖДОЙ КАРТИНКИ
        # ----------------------------------------------------

        results = []

        for index, image in enumerate(

            images,

            1

        ):

            await processing_message.edit_text(

                "🖼️ <b>Обрабатываю изображения</b>\n\n"

                f"📷 Задание: {index} из {total}\n\n"

                "👁️ Читаю текст на картинке...\n"

                "🧠 Разбираю задание...",

                parse_mode="HTML"

            )

            try:

                image_bytes, content_type = (

                    await asyncio.to_thread(

                        download_google_image,

                        image["url"]

                    )

                )

                analysis = (

                    await asyncio.to_thread(

                        analyze_task_image,

                        image_bytes,

                        content_type

                    )

                )

                results.append(

                    format_google_image_result(

                        image,

                        analysis

                    )

                )

            except Exception as e:

                print(

                    f"Ошибка обработки картинки: {e}"

                )

                results.append(

                    f"📷 <b>Задание "

                    f"{image.get('number', '?')}</b>\n\n"

                    "❌ Не удалось обработать "

                    "это изображение."

                )

        # ----------------------------------------------------
        # УДАЛЯЕМ СТАТУС
        # ----------------------------------------------------

        await processing_message.delete()

        # ----------------------------------------------------
        # ОТПРАВЛЯЕМ РЕЗУЛЬТАТЫ
        # ----------------------------------------------------

        for result in results:

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

        if status == 401:

            message = (

                "🔐 <b>Google авторизация истекла.</b>\n\n"

                "Используйте /google."

            )

        elif status == 403:

            message = (

                "❌ <b>Нет доступа к форме.</b>\n\n"

                "Подключённый Google-аккаунт "

                "должен иметь доступ к форме."

            )

        elif status == 404:

            message = (

                "❌ <b>Google Form не найдена.</b>\n\n"

                "Проверьте ссылку."

            )

        else:

            message = (

                "❌ Google Forms API "

                "вернул ошибку."

            )

        await processing_message.edit_text(

            message,

            parse_mode="HTML"

        )

    except Exception as e:

        print(

            f"Google Forms error: {e}"

        )

        await processing_message.edit_text(

            "❌ <b>Ошибка обработки Google Form.</b>\n\n"

            "Попробуйте ещё раз.",

            parse_mode="HTML"

        )


# ============================================================
# SKYSMART HANDLER
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

            "на тест Skysmart."

        )

        return

    processing_message = (

        await update.message.reply_text(

            "⏳ Получаю задания..."

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

    except Exception as e:

        print(

            f"Skysmart error: {e}"

        )

        await processing_message.edit_text(

            "❌ Не удалось обработать тест."

        )


# ============================================================
# GOOGLE COMMAND
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

    if user_id != OWNER_ID:

        await update.message.reply_text(

            "⛔ Подключать Google "

            "может только владелец."

        )

        return

    state = secrets.token_urlsafe(

        32

    )

    if not save_google_oauth_state(

        state,

        user_id

    ):

        await update.message.reply_text(

            "❌ Не удалось создать OAuth-сессию."

        )

        return

    flow = create_google_flow(

        state=state

    )

    authorization_url, _ = (

        flow.authorization_url(

            access_type="offline",

            prompt="consent",

            include_granted_scopes="true"

        )

    )

    keyboard = InlineKeyboardMarkup(

        [

            [

                InlineKeyboardButton(

                    "🔐 Подключить Google",

                    url=authorization_url

                )

            ]

        ]

    )

    await update.message.reply_text(

        "🔐 <b>Подключение Google</b>\n\n"

        "Нажмите кнопку ниже.",

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

            <h2>❌ Авторизация отменена</h2>

            <p>{html.escape(error)}</p>

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

            "❌ Неверный OAuth callback.",

            status_code=400

        )

    oauth_state = get_google_oauth_state(

        state

    )

    if not oauth_state:

        return HTMLResponse(

            "❌ OAuth-сессия истекла.",

            status_code=400

        )

    try:

        flow = create_google_flow(

            state=state

        )

        flow.fetch_token(

            authorization_response=str(

                request.url

            )

        )

        credentials = flow.credentials

        save_google_credentials(

            credentials

        )

        delete_google_oauth_state(

            state

        )

        return HTMLResponse(

            """

            <html>

            <head>

            <meta charset="utf-8">

            <title>Google подключён</title>

            </head>

            <body>

            <h2>✅ Google подключён!</h2>

            <p>Теперь можно вернуться в Telegram.</p>

            </body>

            </html>

            """

        )

    except Exception as e:

        print(

            f"Google callback error: {e}"

        )

        return HTMLResponse(

            "❌ Ошибка авторизации.",

            status_code=500

        )


# ============================================================
# START
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

            "Выберите режим:",

            parse_mode="HTML",

            reply_markup=MAIN_KEYBOARD

        )

        return

    await update.message.reply_text(

        "🔐 Введите пароль:"

    )


# ============================================================
# ID
# ============================================================

async def id_command(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE

):

    await update.message.reply_text(

        f"Ваш Telegram ID:\n"

        f"`{update.effective_user.id}`",

        parse_mode="Markdown"

    )


# ============================================================
# KEYS
# ============================================================

async def keys_command(

    update: Update,

    context: ContextTypes.DEFAULT_TYPE

):

    if update.effective_user.id != OWNER_ID:

        await update.message.reply_text(

            "⛔ Нет доступа."

        )

        return

    result = (

        supabase

        .table("bot_passwords")

        .select("*")

        .eq(

            "used",

            False

        )

        .execute()

    )

    passwords = result.data or []

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


# ============================================================
# HELP
# ============================================================

async def show_help(
    update: Update
):

    await update.message.reply_text(

        "ℹ️ <b>Помощь</b>\n\n"

        "📚 <b>Skysmart</b>\n"

        "Отправьте ссылку на тест.\n\n"

        "📄 <b>Google Forms</b>\n"

        "Отправьте ссылку на форму.\n\n"

        "🖼️ Если в форме есть изображения, "

        "бот попробует прочитать задания "

        "и объяснить их решение.",

        parse_mode="HTML",

        reply_markup=MAIN_KEYBOARD

    )


# ============================================================
# MESSAGE HANDLER
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

    text = (

        update.message.text

        or ""

    ).strip()

    if not text:

        return

    # --------------------------------------------------------
    # АВТОРИЗАЦИЯ
    # --------------------------------------------------------

    if not is_authorized(

        user_id

    ):

        if use_password(

            text,

            user_id

        ):

            await update.message.reply_text(

                "✅ Авторизация успешна!\n\n"

                "Выберите режим:",

                reply_markup=MAIN_KEYBOARD

            )

        else:

            await update.message.reply_text(

                "❌ Неверный пароль."

            )

        return

    # --------------------------------------------------------
    # SKYSMART MODE
    # --------------------------------------------------------

    if text == "📚 Skysmart":

        set_user_mode(

            user_id,

            "skysmart"

        )

        await update.message.reply_text(

            "📚 <b>Режим Skysmart выбран.</b>\n\n"

            "Отправьте ссылку.",

            parse_mode="HTML"

        )

        return

    # --------------------------------------------------------
    # GOOGLE FORMS MODE
    # --------------------------------------------------------

    if text == "📄 Google Forms":

        set_user_mode(

            user_id,

            "google_forms"

        )

        await update.message.reply_text(

            "📄 <b>Режим Google Forms выбран.</b>\n\n"

            "Отправьте ссылку на Google Form.",

            parse_mode="HTML"

        )

        return

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    if text == "ℹ️ Помощь":

        await show_help(

            update

        )

        return

    # --------------------------------------------------------
    # CURRENT MODE
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
# HANDLERS
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

        filters.TEXT

        &

        ~filters.COMMAND,

        message_handler

    )

)


# ============================================================
# TELEGRAM WEBHOOK
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
# HOME
# ============================================================

async def homepage(

    request: Request
):

    return PlainTextResponse(

        "Telegram bot is running."

    )


# ============================================================
# HEALTH
# ============================================================

async def health(

    request: Request
):

    return PlainTextResponse(

        "OK"

    )


# ============================================================
# STARTUP
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

    initialize_passwords()

    cleanup_google_oauth_states()

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

        f"Webhook установлен:\n{webhook_url}"

    )

    print(

        "Бот успешно запущен!"

    )


# ============================================================
# SHUTDOWN
# ============================================================

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


# ============================================================
# STARLETTE APP
# ============================================================

app = Starlette(

    routes=[

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

    ],

    on_startup=[

        startup

    ],

    on_shutdown=[

        shutdown

    ]

)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(

        app,

        host="0.0.0.0",

        port=PORT

    )
