import os
import re
import json
import hashlib
import secrets
import logging
import asyncio
import base64

import aiohttp
import uvicorn

from bs4 import BeautifulSoup
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
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
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

RENDER_URL = (
    os.getenv("RENDER_EXTERNAL_URL")
    or os.getenv("RENDER_URL")
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = "/telegram"

SKYSMART_ROOM_URL = (
    "https://api-edu.skysmart.ru/api/v1/task/preview"
)

SKYSMART_AUTH_URL = (
    "https://api-edu.skysmart.ru/api/v1/user/registration/teacher"
)

SKYSMART_STEP_URL = (
    "https://api-edu.skysmart.ru/api/v1/content/step/load?stepUuid="
)


# ============================================================
# STARTUP CHECK
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

if not RENDER_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL / RENDER_URL не задан")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL не задан")

if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY не задан")


# ============================================================
# SUPABASE
# ============================================================

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
)


# ============================================================
# USER STATES
# ============================================================

user_states = {}


# ============================================================
# PASSWORD / AUTH
# ============================================================

def hash_password(password: str) -> str:
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def generate_password() -> str:
    part1 = secrets.token_hex(2).upper()
    part2 = secrets.token_hex(2).upper()
    return f"{part1}-{part2}"


def create_passwords(count: int = 10):
    created = []

    for _ in range(count):
        password = generate_password()
        password_hash = hash_password(password)

        supabase.table("bot_passwords").insert({
            "password_hash": password_hash,
            "used": False,
        }).execute()

        created.append(password)

    return created


def check_password(password: str) -> bool:
    password_hash = hash_password(password)

    result = (
        supabase
        .table("bot_passwords")
        .select("*")
        .eq("password_hash", password_hash)
        .eq("used", False)
        .limit(1)
        .execute()
    )

    return bool(result.data)


def mark_password_used(password: str, user_id: int):
    password_hash = hash_password(password)

    (
        supabase
        .table("bot_passwords")
        .update({
            "used": True,
            "used_by": str(user_id),
        })
        .eq("password_hash", password_hash)
        .execute()
    )


def get_user(user_id: int):
    result = (
        supabase
        .table("bot_users")
        .select("*")
        .eq("telegram_id", str(user_id))
        .limit(1)
        .execute()
    )

    if result.data:
        return result.data[0]

    return None


def create_user(user_id: int):
    existing = get_user(user_id)

    if existing:
        return existing

    result = (
        supabase
        .table("bot_users")
        .insert({
            "telegram_id": str(user_id),
        })
        .execute()
    )

    if result.data:
        return result.data[0]

    return None


def authorize_user(user_id: int):
    user = get_user(user_id)

    if not user:
        return False

    return bool(user.get("authorized", False))


def set_user_authorized(user_id: int):
    (
        supabase
        .table("bot_users")
        .update({
            "authorized": True,
        })
        .eq("telegram_id", str(user_id))
        .execute()
    )


# ============================================================
# KEYBOARDS
# ============================================================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [
            KeyboardButton("📚 Получить ответы"),
        ],
        [
            KeyboardButton("🆔 Мой ID"),
        ],
    ],
    resize_keyboard=True,
)


# ============================================================
# SKYSMART API CLIENT
# ============================================================

class SkysmartAPIClient:
    """
    Клиент Skysmart API.

    Архитектура оставлена такой же:
        authenticate()
        ↓
        get_room()
        ↓
        get_task_html()
    """

    def __init__(self):
        self.session = None
        self.token = ""

    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=60)

            self.session = aiohttp.ClientSession(
                timeout=timeout
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def authenticate(self):
        await self._ensure_session()

        headers = {
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/153.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }

        logger.info("Skysmart AUTH: отправляем запрос")

        async with self.session.post(
            SKYSMART_AUTH_URL,
            headers=headers,
        ) as response:

            logger.info(
                "Skysmart AUTH STATUS: %s",
                response.status,
            )

            text = await response.text()

            if response.status != 200:
                logger.error(
                    "Skysmart AUTH ERROR: %s",
                    text[:1000],
                )

                raise Exception(
                    f"Authentication failed with status: "
                    f"{response.status}"
                )

            try:
                data = json.loads(text)
            except Exception:
                raise Exception(
                    "Skysmart AUTH: сервер вернул "
                    "не JSON"
                )

            self.token = (
                data.get("jwtToken")
                or data.get("token")
                or data.get("accessToken")
                or ""
            )

            if not self.token:
                raise Exception(
                    "Skysmart AUTH: JWT token не найден"
                )

            logger.info(
                "Skysmart AUTH: token получен"
            )

    async def _get_headers(self):
        if not self.token:
            await self.authenticate()

        return {
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/153.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "application/json, text/plain, */*"
            ),
            "Authorization": f"Bearer {self.token}",
        }

    async def get_room(self, task_hash: str):
        await self._ensure_session()

        payload = {
            "taskHash": task_hash,
        }

        headers = await self._get_headers()

        logger.info(
            "Skysmart get_room: %s",
            task_hash,
        )

        async with self.session.post(
            SKYSMART_ROOM_URL,
            headers=headers,
            json=payload,
        ) as response:

            logger.info(
                "Skysmart get_room STATUS: %s",
                response.status,
            )

            text = await response.text()

            if response.status != 200:
                logger.error(
                    "get_room ERROR: %s",
                    text[:1000],
                )

                raise Exception(
                    f"get_room failed with status: "
                    f"{response.status}"
                )

            data = json.loads(text)

            step_uuids = (
                data
                .get("meta", {})
                .get("stepUuids")
            )

            if not step_uuids:
                step_uuids = (
                    data
                    .get("data", {})
                    .get("stepUuids")
                )

            if not step_uuids:
                raise Exception(
                    "В ответе Skysmart не найдены "
                    "stepUuids"
                )

            logger.info(
                "Найдено заданий: %s",
                len(step_uuids),
            )

            return step_uuids

    async def get_task_html(self, uuid: str):
        await self._ensure_session()

        headers = await self._get_headers()

        url = SKYSMART_STEP_URL + uuid

        async with self.session.get(
            url,
            headers=headers,
        ) as response:

            if response.status != 200:
                text = await response.text()

                logger.error(
                    "get_task_html ERROR %s: %s",
                    response.status,
                    text[:500],
                )

                raise Exception(
                    f"get_task_html failed with status: "
                    f"{response.status}"
                )

            data = await response.json()

            content = data.get("content", "")

            if not content:
                logger.warning(
                    "Пустой content для UUID %s",
                    uuid,
                )

            return content


# ============================================================
# TEXT HELPERS
# ============================================================

def remove_extra_newlines(text: str) -> str:
    if not text:
        return ""

    return re.sub(
        r"\n+",
        "\n",
        text.strip(),
    )


def clean_answer_text(text: str) -> str:
    """
    Минимальная очистка ответа.

    Важно:
    математические выражения не переписываем,
    регистр не меняем.
    """

    if text is None:
        return ""

    text = str(text)

    text = text.replace("\xa0", " ")

    return text.strip()


def decode_base64_text(value: str):
    if not value:
        return None

    try:
        return base64.b64decode(
            value
        ).decode("utf-8")
    except Exception as e:
        logger.warning(
            "Ошибка base64 decode: %s",
            e,
        )
        return None


# ============================================================
# ROOM NAME
# ============================================================

def extract_room_name(text: str):
    if not text:
        return None

    match = re.search(
        r"edu\.skysmart\.ru/student/([^/?#]+)",
        text,
    )

    if match:
        return match.group(1)

    return None


# ============================================================
# DIAGNOSTICS
# ============================================================

def log_task_structure(soup: BeautifulSoup, task_number: int):
    """
    Показывает в Render, какие Skysmart-компоненты
    реально присутствуют внутри задания.

    Особенно полезно, если answers == [].
    """

    tags = [
        "vim-test-item",
        "vim-order-sentence-verify-item",
        "vim-input-answers",
        "vim-input-item",
        "vim-select-item",
        "vim-test-image-item",
        "math-input-answer",
        "vim-dnd-text-drop",
        "vim-dnd-text-drag",
        "vim-dnd-group-drag",
        "vim-dnd-group-item",
        "vim-groups-row",
        "vim-groups-item",
        "vim-strike-out-item",
        "vim-dnd-image-set-drag",
        "vim-dnd-image-set-drop",
        "vim-dnd-image-drag",
        "vim-dnd-image-drop",
        "edu-open-answer",
    ]

    found = {}

    for tag in tags:
        count = len(soup.find_all(tag))

        if count:
            found[tag] = count

    logger.info(
        "TASK %s STRUCTURE: %s",
        task_number,
        found,
    )


# ============================================================
# QUESTION
# ============================================================

def extract_task_question(soup: BeautifulSoup):
    instruction = soup.find(
        "vim-instruction"
    )

    if instruction:
        return instruction.get_text(
            " ",
            strip=True,
        )

    return ""


def extract_task_full_question(soup: BeautifulSoup):
    soup_copy = BeautifulSoup(
        str(soup),
        "html.parser",
    )

    elements_to_exclude = [
        "vim-instruction",
        "vim-groups",
        "vim-test-item",
        "vim-order-sentence-verify-item",
        "vim-input-answers",
        "vim-select-item",
        "vim-test-image-item",
        "math-input-answer",
        "vim-dnd-text-drop",
        "vim-dnd-group-drag",
        "vim-groups-row",
        "vim-strike-out-item",
        "vim-dnd-image-set-drag",
        "vim-dnd-image-drag",
        "vim-dnd-image-set-drop",
        "vim-dnd-image-drop",
        "edu-open-answer",
    ]

    for element in soup_copy.find_all(
        elements_to_exclude
    ):
        element.decompose()

    return remove_extra_newlines(
        soup_copy.get_text("\n")
    )


# ============================================================
# ANSWER EXTRACTION
# ============================================================

def extract_task_answer(
    soup: BeautifulSoup,
    task_number: int,
):
    """
    Извлекает ответы из HTML задания.

    Логика максимально приближена к SkyAnswers._get_task_answer()
    из рабочего skysmart-answers.

    Дополнительно сохранены расширенные типы заданий.
    """

    answers = []

    # --------------------------------------------------------
    # DIAGNOSTICS
    # --------------------------------------------------------

    log_task_structure(
        soup,
        task_number,
    )

    # --------------------------------------------------------
    # 1. MULTIPLE CHOICE
    # SkyAnswers:
    #
    # soup.find_all(
    #     'vim-test-item',
    #     attrs={'correct': 'true'}
    # )
    # --------------------------------------------------------

    for item in soup.find_all(
        "vim-test-item",
        attrs={"correct": "true"},
    ):
        text = clean_answer_text(
            item.get_text(
                " ",
                strip=True,
            )
        )

        if text:
            answers.append(text)

    # --------------------------------------------------------
    # 2. ORDER SENTENCE
    # --------------------------------------------------------

    for item in soup.find_all(
        "vim-order-sentence-verify-item"
    ):
        text = clean_answer_text(
            item.get_text(
                " ",
                strip=True,
            )
        )

        if text:
            answers.append(text)

    # --------------------------------------------------------
    # 3. INPUT ANSWERS
    # SkyAnswers сначала ищет vim-input-answers,
    # затем vim-input-item.
    # --------------------------------------------------------

    for input_answer in soup.find_all(
        "vim-input-answers"
    ):
        input_item = input_answer.find(
            "vim-input-item"
        )

        if input_item:
            text = clean_answer_text(
                input_item.get_text(
                    " ",
                    strip=True,
                )
            )

            if text:
                answers.append(text)

    # --------------------------------------------------------
    # Дополнительный вариант:
    # если vim-input-item есть напрямую.
    # --------------------------------------------------------

    if not soup.find("vim-input-answers"):
        for input_item in soup.find_all(
            "vim-input-item"
        ):
            text = clean_answer_text(
                input_item.get_text(
                    " ",
                    strip=True,
                )
            )

            if text:
                answers.append(text)

    # --------------------------------------------------------
    # 4. SELECT ITEMS
    # --------------------------------------------------------

    for select_item in soup.find_all(
        "vim-select-item",
        attrs={"correct": "true"},
    ):
        text = clean_answer_text(
            select_item.get_text(
                " ",
                strip=True,
            )
        )

        if text:
            answers.append(text)

    # --------------------------------------------------------
    # 5. TEST IMAGE ITEMS
    # --------------------------------------------------------

    for image_item in soup.find_all(
        "vim-test-image-item",
        attrs={"correct": "true"},
    ):
        text = clean_answer_text(
            image_item.get_text(
                " ",
                strip=True,
            )
        )

        if text:
            answers.append(
                f"{text} - Correct"
            )

    # --------------------------------------------------------
    # 6. MATH INPUT ANSWERS
    # --------------------------------------------------------

    for math_answer in soup.find_all(
        "math-input-answer"
    ):
        text = clean_answer_text(
            math_answer.get_text(
                " ",
                strip=True,
            )
        )

        if text:
            answers.append(text)

    # --------------------------------------------------------
    # 7. DND TEXT
    # --------------------------------------------------------

    for drop in soup.find_all(
        "vim-dnd-text-drop"
    ):
        drag_ids_raw = (
            drop.get("drag-ids", "")
        )

        drag_ids = [
            x.strip()
            for x in drag_ids_raw.split(",")
            if x.strip()
        ]

        for drag_id in drag_ids:
            drag = soup.find(
                "vim-dnd-text-drag",
                attrs={
                    "answer-id": drag_id
                },
            )

            if drag:
                text = clean_answer_text(
                    drag.get_text(
                        " ",
                        strip=True,
                    )
                )

                if text:
                    answers.append(text)

    # --------------------------------------------------------
    # 8. DND GROUPS
    # --------------------------------------------------------

    for drag_group in soup.find_all(
        "vim-dnd-group-drag"
    ):
        answer_id = drag_group.get(
            "answer-id"
        )

        drag_group_text = clean_answer_text(
            drag_group.get_text(
                " ",
                strip=True,
            )
        )

        for group_item in soup.find_all(
            "vim-dnd-group-item"
        ):
            drag_ids_raw = (
                group_item.get(
                    "drag-ids",
                    "",
                )
            )

            drag_ids = [
                x.strip()
                for x in drag_ids_raw.split(",")
                if x.strip()
            ]

            if answer_id in drag_ids:
                group_text = clean_answer_text(
                    group_item.get_text(
                        " ",
                        strip=True,
                    )
                )

                if group_text:
                    answers.append(
                        f"{group_text} - "
                        f"{drag_group_text}"
                    )

    # --------------------------------------------------------
    # 9. GROUP ROWS
    # --------------------------------------------------------

    for group_row in soup.find_all(
        "vim-groups-row"
    ):
        for group_item in group_row.find_all(
            "vim-groups-item"
        ):
            encoded_text = group_item.get(
                "text"
            )

            if encoded_text:
                decoded_text = decode_base64_text(
                    encoded_text
                )

                if decoded_text:
                    answers.append(
                        clean_answer_text(
                            decoded_text
                        )
                    )

    # --------------------------------------------------------
    # 10. STRIKE OUT
    # --------------------------------------------------------

    for striked_item in soup.find_all(
        "vim-strike-out-item",
        attrs={"striked": "true"},
    ):
        text = clean_answer_text(
            striked_item.get_text(
                " ",
                strip=True,
            )
        )

        if text:
            answers.append(text)

    # --------------------------------------------------------
    # 11. DND IMAGE SET
    # --------------------------------------------------------

    for image_drag in soup.find_all(
        "vim-dnd-image-set-drag"
    ):
        answer_id = image_drag.get(
            "answer-id"
        )

        drag_text = clean_answer_text(
            image_drag.get_text(
                " ",
                strip=True,
            )
        )

        for image_drop in soup.find_all(
            "vim-dnd-image-set-drop"
        ):
            drag_ids_raw = image_drop.get(
                "drag-ids",
                "",
            )

            drag_ids = [
                x.strip()
                for x in drag_ids_raw.split(",")
                if x.strip()
            ]

            if answer_id in drag_ids:

                image_value = (
                    image_drop.get("image")
                    or image_drop.get("src")
                    or ""
                )

                image_value = clean_answer_text(
                    image_value
                )

                if image_value:
                    answers.append(
                        f"{image_value} - "
                        f"{drag_text}"
                    )
                elif drag_text:
                    answers.append(
                        drag_text
                    )

    # --------------------------------------------------------
    # 12. DND IMAGES
    # --------------------------------------------------------

    for image_drag in soup.find_all(
        "vim-dnd-image-drag"
    ):
        answer_id = image_drag.get(
            "answer-id"
        )

        drag_text = clean_answer_text(
            image_drag.get_text(
                " ",
                strip=True,
            )
        )

        for image_drop in soup.find_all(
            "vim-dnd-image-drop"
        ):
            drag_ids_raw = image_drop.get(
                "drag-ids",
                "",
            )

            drag_ids = [
                x.strip()
                for x in drag_ids_raw.split(",")
                if x.strip()
            ]

            if answer_id in drag_ids:
                drop_text = clean_answer_text(
                    image_drop.get_text(
                        " ",
                        strip=True,
                    )
                )

                if drop_text and drag_text:
                    answers.append(
                        f"{drop_text} - "
                        f"{drag_text}"
                    )
                elif drag_text:
                    answers.append(
                        drag_text
                    )
                elif drop_text:
                    answers.append(
                        drop_text
                    )

    # --------------------------------------------------------
    # 13. OPEN ANSWER
    # --------------------------------------------------------

    if soup.find(
        "edu-open-answer",
        attrs={"id": "OA1"},
    ):
        answers.append(
            "File upload required"
        )

    # --------------------------------------------------------
    # DEDUPLICATION
    #
    # Важно: порядок сохраняем.
    # --------------------------------------------------------

    unique_answers = []

    for answer in answers:
        if answer not in unique_answers:
            unique_answers.append(answer)

    answers = unique_answers

    # --------------------------------------------------------
    # RESULT LOG
    # --------------------------------------------------------

    logger.info(
        "TASK %s ANSWERS COUNT: %s",
        task_number,
        len(answers),
    )

    logger.info(
        "TASK %s ANSWERS: %s",
        task_number,
        answers,
    )

    if not answers:
        logger.warning(
            "TASK %s: answers=[]",
            task_number,
        )

        # Небольшая диагностика содержимого.
        text_preview = soup.get_text(
            " ",
            strip=True,
        )

        logger.warning(
            "TASK %s TEXT PREVIEW: %s",
            task_number,
            text_preview[:1000],
        )

    return {
        "question": extract_task_question(
            soup
        ),
        "full_question": extract_task_full_question(
            soup
        ),
        "answers": answers,
        "task_number": task_number,
    }


# ============================================================
# LOAD ALL TASKS
# ============================================================

async def load_all_tasks(
    room_name: str,
):
    client = SkysmartAPIClient()

    try:
        step_uuids = await client.get_room(
            room_name
        )

        logger.info(
            "Получено step UUID: %s",
            len(step_uuids),
        )

        # ----------------------------------------------------
        # Получаем HTML параллельно
        # ----------------------------------------------------

        tasks_html = await asyncio.gather(
            *[
                client.get_task_html(uuid)
                for uuid in step_uuids
            ],
            return_exceptions=True,
        )

        result = []

        for index, task_html in enumerate(
            tasks_html
        ):
            task_number = index + 1

            if isinstance(
                task_html,
                Exception,
            ):
                logger.error(
                    "TASK %s ERROR: %s",
                    task_number,
                    task_html,
                )

                result.append({
                    "question": "",
                    "full_question": "",
                    "answers": [],
                    "task_number": task_number,
                })

                continue

            if not task_html:
                logger.warning(
                    "TASK %s: пустой HTML",
                    task_number,
                )

                result.append({
                    "question": "",
                    "full_question": "",
                    "answers": [],
                    "task_number": task_number,
                })

                continue

            soup = BeautifulSoup(
                task_html,
                "html.parser",
            )

            task_answer = extract_task_answer(
                soup,
                task_number,
            )

            result.append(
                task_answer
            )

        return result

    finally:
        await client.close()


# ============================================================
# SYNCHRONOUS WRAPPER
# ============================================================

def get_skysmart_answers(
    room_name: str,
):
    return asyncio.run(
        load_all_tasks(room_name)
    )


# ============================================================
# BUILD ANSWER ARRAY
# ============================================================

def build_all_tasks_answers(data):
    """
    Преобразует:

    [
        {
            "task_number": 1,
            "answers": [...]
        },
        ...
    ]

    в:

    [
        [...],
        [...],
        ...
    ]

    Порядок заданий сохраняется.
    """

    if not data:
        return []

    result = []

    for task in data:
        answers = task.get(
            "answers",
            [],
        )

        if not isinstance(
            answers,
            list,
        ):
            answers = []

        result.append(
            answers
        )

    return result


# ============================================================
# FORMAT
# ============================================================

def format_all_tasks_answers(
    answers,
):
    return json.dumps(
        answers,
        ensure_ascii=False,
        indent=2,
    )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user:
        return

    user_id = user.id

    create_user(user_id)

    if authorize_user(user_id):
        user_states[user_id] = {
            "state": "waiting_link"
        }

        await update.message.reply_text(
            "✅ Вы авторизованы.\n\n"
            "Отправьте ссылку на задание Skysmart.",
            reply_markup=MAIN_KEYBOARD,
        )

        return

    user_states[user_id] = {
        "state": "waiting_password"
    }

    await update.message.reply_text(
        "🔐 Введите пароль доступа:",
        reply_markup=MAIN_KEYBOARD,
    )


async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    await update.message.reply_text(
        f"🆔 Ваш Telegram ID:\n"
        f"`{update.effective_user.id}`",
        parse_mode="Markdown",
    )


async def keys_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user:
        return

    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    result = (
        supabase
        .table("bot_passwords")
        .select("password_hash, used")
        .eq("used", False)
        .execute()
    )

    if not result.data:
        await update.message.reply_text(
            "Свободных ключей нет."
        )
        return

    lines = [
        "🔑 Хэши неиспользованных ключей:",
        "",
    ]

    for item in result.data:
        lines.append(
            item["password_hash"]
        )

    await update.message.reply_text(
        "\n".join(lines)
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    user_id = user.id

    text = (
        update.message.text
        or ""
    ).strip()

    # --------------------------------------------------------
    # MY ID
    # --------------------------------------------------------

    if text == "🆔 Мой ID":
        await id_command(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # CREATE USER
    # --------------------------------------------------------

    create_user(user_id)

    # --------------------------------------------------------
    # AUTH
    # --------------------------------------------------------

    if not authorize_user(user_id):

        if check_password(text):
            mark_password_used(
                text,
                user_id,
            )

            set_user_authorized(
                user_id
            )

            user_states[user_id] = {
                "state": "waiting_link"
            }

            await update.message.reply_text(
                "✅ Пароль принят.\n\n"
                "Теперь отправьте ссылку на "
                "задание Skysmart.",
                reply_markup=MAIN_KEYBOARD,
            )

            return

        await update.message.reply_text(
            "❌ Неверный пароль.\n\n"
            "Введите пароль доступа."
        )

        return

    # --------------------------------------------------------
    # BUTTON
    # --------------------------------------------------------

    if text == "📚 Получить ответы":
        user_states[user_id] = {
            "state": "waiting_link"
        }

        await update.message.reply_text(
            "🔗 Отправьте ссылку на задание Skysmart."
        )

        return

    # --------------------------------------------------------
    # ROOM NAME
    # --------------------------------------------------------

    room_name = extract_room_name(
        text
    )

    if not room_name:

        # Иногда пользователь может отправить
        # только roomName.
        if re.fullmatch(
            r"[A-Za-z0-9_-]+",
            text,
        ):
            room_name = text

    if not room_name:
        await update.message.reply_text(
            "❌ Не удалось найти roomName.\n\n"
            "Отправьте ссылку вида:\n"
            "https://edu.skysmart.ru/student/..."
        )

        return

    # --------------------------------------------------------
    # LOADING
    # --------------------------------------------------------

    loading_message = await update.message.reply_text(
        "⏳ Получаю задания и ответы с Skysmart..."
    )

    try:
        data = await asyncio.to_thread(
            get_skysmart_answers,
            room_name,
        )

        all_tasks_answers = (
            build_all_tasks_answers(
                data
            )
        )

        output = format_all_tasks_answers(
            all_tasks_answers
        )

        if not output:
            output = "[]"

        # ----------------------------------------------------
        # SEND CHUNKS
        # ----------------------------------------------------

        max_length = 3900

        chunks = [
            output[i:i + max_length]
            for i in range(
                0,
                len(output),
                max_length,
            )
        ]

        await loading_message.edit_text(
            "✅ Готово."
        )

        for chunk in chunks:
            await update.message.reply_text(
                f"```json\n{chunk}\n```",
                parse_mode="Markdown",
            )

    except Exception as e:
        logger.exception(
            "Ошибка получения ответов"
        )

        try:
            await loading_message.edit_text(
                "❌ Не удалось получить ответы."
            )
        except Exception:
            pass

        await update.message.reply_text(
            "Подробность ошибки записана в Render Logs."
        )


# ============================================================
# WEBHOOK
# ============================================================

telegram_app = None


async def telegram_webhook(
    request: Request,
):
    try:
        data = await request.json()

        update = Update.de_json(
            data,
            telegram_app.bot,
        )

        await telegram_app.process_update(
            update
        )

        return JSONResponse({
            "ok": True
        })

    except Exception as e:
        logger.exception(
            "Ошибка webhook: %s",
            e,
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e),
            },
            status_code=500,
        )


# ============================================================
# HEALTH
# ============================================================

async def health(
    request: Request,
):
    return PlainTextResponse(
        "OK"
    )


async def root(
    request: Request,
):
    return PlainTextResponse(
        "Skysmart Telegram Bot is running"
    )


# ============================================================
# STARLETTE
# ============================================================

routes = [
    Route(
        "/",
        root,
        methods=["GET"],
    ),
    Route(
        "/health",
        health,
        methods=["GET"],
    ),
    Route(
        WEBHOOK_PATH,
        telegram_webhook,
        methods=["POST"],
    ),
]

app = Starlette(
    routes=routes
)


# ============================================================
# TELEGRAM STARTUP
# ============================================================

async def startup():
    global telegram_app

    logger.info(
        "Запуск Telegram application..."
    )

    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "id",
            id_command,
        )
    )

    telegram_app.add_handler(
        CommandHandler(
            "keys",
            keys_command,
        )
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            message_handler,
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()

    webhook_url = (
        RENDER_URL.rstrip("/")
        + WEBHOOK_PATH
    )

    logger.info(
        "Устанавливаем Telegram webhook: %s",
        webhook_url,
    )

    await telegram_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=[
            "message",
            "edited_message",
            "callback_query",
        ],
    )

    logger.info(
        "Telegram webhook успешно установлен!"
    )


async def shutdown():
    global telegram_app

    if telegram_app:
        try:
            await telegram_app.stop()
        except Exception:
            pass

        try:
            await telegram_app.shutdown()
        except Exception:
            pass


app.add_event_handler(
    "startup",
    startup,
)

app.add_event_handler(
    "shutdown",
    shutdown,
)


# ============================================================
# LOCAL ENTRY POINT
# ============================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
    )
