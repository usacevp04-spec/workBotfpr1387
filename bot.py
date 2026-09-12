import os
import re
import json
import time
import hashlib
import secrets
import logging
import asyncio
import base64

import requests
import aiohttp
import uvicorn

from bs4 import BeautifulSoup
from datetime import datetime, timezone

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
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))

PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = "/telegram"


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# SUPABASE
# ============================================================

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
)


# ============================================================
# СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================

user_states = {}


# ============================================================
# ПАРОЛИ
# ============================================================

PASSWORD_ALPHABET = (
    "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
)


def password_keyboard():
    return ReplyKeyboardMarkup(
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


def main_keyboard():
    return ReplyKeyboardMarkup(
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


def generate_password():
    part1 = "".join(
        secrets.choice(PASSWORD_ALPHABET)
        for _ in range(4)
    )

    part2 = "".join(
        secrets.choice(PASSWORD_ALPHABET)
        for _ in range(4)
    )

    return f"{part1}-{part2}"


def hash_password(password):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def initialize_passwords():
    try:
        result = (
            supabase
            .table("bot_passwords")
            .select("id")
            .limit(1)
            .execute()
        )

        if result.data:
            logger.info(
                "Пароли уже существуют."
            )
            return

        rows = []

        for _ in range(10):
            password = generate_password()

            rows.append(
                {
                    "password_hash": hash_password(
                        password
                    ),
                    "used": False,
                }
            )

        supabase.table(
            "bot_passwords"
        ).insert(rows).execute()

        logger.info(
            "Созданы новые пароли."
        )

    except Exception as e:
        logger.exception(
            "Ошибка initialize_passwords: %s",
            e,
        )


def create_user_if_needed(user_id):
    try:
        result = (
            supabase
            .table("bot_users")
            .select("*")
            .eq("telegram_id", user_id)
            .execute()
        )

        if result.data:
            return result.data[0]

        result = (
            supabase
            .table("bot_users")
            .insert(
                {
                    "telegram_id": user_id,
                    "authorized": False,
                }
            )
            .execute()
        )

        if result.data:
            return result.data[0]

    except Exception as e:
        logger.exception(
            "Ошибка create_user_if_needed: %s",
            e,
        )

    return None


def is_authorized(user_id):
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
            result.data[0].get(
                "authorized",
                False,
            )
        )

    except Exception as e:
        logger.exception(
            "Ошибка is_authorized: %s",
            e,
        )

        return False


def use_password(user_id, password):
    password = password.strip().upper()

    password_hash = hash_password(
        password
    )

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

        supabase.table(
            "bot_passwords"
        ).update(
            {
                "used": True,
                "used_by": user_id,
                "used_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
        ).eq(
            "id",
            password_row["id"],
        ).execute()

        supabase.table(
            "bot_users"
        ).upsert(
            {
                "telegram_id": user_id,
                "authorized": True,
            },
            on_conflict="telegram_id",
        ).execute()

        return True

    except Exception as e:
        logger.exception(
            "Ошибка use_password: %s",
            e,
        )

        return False


def make_password_list():
    try:
        result = (
            supabase
            .table("bot_passwords")
            .select("*")
            .eq("used", False)
            .execute()
        )

        if not result.data:
            return "Свободных паролей нет."

        passwords = []

        for row in result.data:
            password_hash = row.get(
                "password_hash"
            )

            if not password_hash:
                continue

            passwords.append(
                password_hash
            )

        return "\n".join(passwords)

    except Exception as e:
        logger.exception(
            "Ошибка make_password_list: %s",
            e,
        )

        return "Ошибка получения паролей."


# ============================================================
# SKySMART
# ============================================================

SKYSMART_ROOM_URL = (
    "https://api-edu.skysmart.ru/"
    "api/v1/task/preview"
)

SKYSMART_AUTH_URL = (
    "https://api-edu.skysmart.ru/"
    "api/v1/user/registration/teacher"
)

SKYSMART_STEP_URL = (
    "https://api-edu.skysmart.ru/"
    "api/v1/content/step/load?stepUuid="
)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def clean_text(text):
    if not text:
        return ""

    text = str(text)

    text = text.replace(
        "\r\n",
        "\n",
    )

    text = text.replace(
        "\r",
        "\n",
    )

    lines = []

    for line in text.split("\n"):
        line = line.strip()

        if line:
            lines.append(line)

    return "\n".join(lines).strip()


def remove_extra_newlines(text):
    if not text:
        return ""

    return re.sub(
        r"\n+",
        "\n",
        text.strip(),
    )


# ============================================================
# ИЗВЛЕЧЕНИЕ ROOM NAME
# ============================================================

def extract_room_name(url):
    if not url:
        return None

    match = re.search(
        r"edu\.skysmart\.ru/student/([^/?#]+)",
        url,
    )

    if match:
        return match.group(1)

    return None


# ============================================================
# SKYSMART API CLIENT
# ============================================================

class SkysmartAPIClient:

    def __init__(self):
        self.session = None
        self.jwt_token = None

    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(
            total=60
        )

        self.session = aiohttp.ClientSession(
            timeout=timeout
        )

        return self

    async def __aexit__(
        self,
        exc_type,
        exc,
        tb,
    ):
        if self.session:
            await self.session.close()

    async def authenticate(self):
        try:
            async with self.session.post(
                SKYSMART_AUTH_URL,
                json={},
            ) as response:

                logger.info(
                    "AUTH STATUS: %s",
                    response.status,
                )

                text = await response.text()

                if response.status != 200:
                    logger.error(
                        "AUTH ERROR: %s",
                        text[:1000],
                    )
                    return False

                try:
                    data = json.loads(text)
                except Exception:
                    logger.error(
                        "Не удалось распарсить AUTH JSON."
                    )
                    return False

                self.jwt_token = (
                    data.get("jwtToken")
                    or data.get("token")
                )

                if not self.jwt_token:
                    logger.error(
                        "JWT token не найден."
                    )
                    return False

                return True

        except Exception as e:
            logger.exception(
                "Ошибка authenticate: %s",
                e,
            )

            return False

    async def get_room(
        self,
        task_hash,
    ):
        headers = {}

        if self.jwt_token:
            headers[
                "Authorization"
            ] = f"Bearer {self.jwt_token}"

        try:
            async with self.session.post(
                SKYSMART_ROOM_URL,
                json={
                    "taskHash": task_hash
                },
                headers=headers,
            ) as response:

                logger.info(
                    "ROOM STATUS: %s",
                    response.status,
                )

                text = await response.text()

                if response.status != 200:
                    logger.error(
                        "ROOM ERROR: %s",
                        text[:1000],
                    )
                    return None

                try:
                    data = json.loads(text)
                except Exception:
                    logger.error(
                        "ROOM response не JSON."
                    )
                    return None

                return data

        except Exception as e:
            logger.exception(
                "Ошибка get_room: %s",
                e,
            )

            return None

    async def get_task_html(
        self,
        uuid,
    ):
        headers = {}

        if self.jwt_token:
            headers[
                "Authorization"
            ] = f"Bearer {self.jwt_token}"

        url = (
            SKYSMART_STEP_URL
            + str(uuid)
        )

        try:
            async with self.session.get(
                url,
                headers=headers,
            ) as response:

                logger.info(
                    "STEP %s STATUS: %s",
                    uuid,
                    response.status,
                )

                text = await response.text()

                if response.status != 200:
                    logger.error(
                        "STEP ERROR %s: %s",
                        uuid,
                        text[:1000],
                    )
                    return None

                try:
                    data = json.loads(text)
                except Exception:
                    logger.error(
                        "STEP response не JSON: %s",
                        uuid,
                    )
                    return None

                return data.get(
                    "content",
                    "",
                )

        except Exception as e:
            logger.exception(
                "Ошибка get_task_html %s: %s",
                uuid,
                e,
            )

            return None


# ============================================================
# ВОПРОС ЗАДАНИЯ
# ============================================================

def extract_task_question(soup):

    instruction = soup.find(
        "vim-instruction"
    )

    if instruction:
        return clean_text(
            instruction.get_text(
                " ",
                strip=True,
            )
        )

    return ""


# ============================================================
# ПОЛНЫЙ ТЕКСТ ЗАДАНИЯ
# ============================================================

def extract_task_full_question(soup):

    soup_copy = BeautifulSoup(
        str(soup),
        "html.parser",
    )

    tags_to_remove = [
        "vim-instruction",
        "vim-groups",
        "vim-test-item",
        "vim-order-sentence-verify-item",
        "vim-input-answers",
        "vim-select-item",
        "vim-test-image-item",
        "math-input-answer",
        "vim-dnd-text-drop",
        "vim-dnd-text-drag",
        "vim-dnd-group-drag",
        "vim-dnd-group-item",
        "vim-groups-row",
        "vim-strike-out-item",
        "vim-dnd-image-set-drag",
        "vim-dnd-image-set-drop",
        "vim-dnd-image-drag",
        "vim-dnd-image-drop",
        "edu-open-answer",
    ]

    for tag_name in tags_to_remove:
        for tag in soup_copy.find_all(
            tag_name
        ):
            tag.decompose()

    text = soup_copy.get_text(
        "\n",
        strip=True,
    )

    return remove_extra_newlines(
        text
    )


# ============================================================
# ИЗВЛЕЧЕНИЕ ОТВЕТОВ
# ============================================================

def extract_task_answer(
    soup,
    task_number,
):
    """
    ВАЖНО:

    Ответы извлекаются строго в порядке DOM.

    То есть если HTML содержит:

        math-input -> 2
        math-input -> 2
        math-input -> 4
        math-input -> 3
        math-input -> 2
        math-input -> 4
        math-input -> 4

    результат будет:

        2 2 4 3 2 4 4

    Никакой сортировки по типам элементов
    здесь больше нет.
    """

    answers = []

    # --------------------------------------------------------
    # Очистка отдельного ответа
    # --------------------------------------------------------

    def clean_answer(value):

        if value is None:
            return ""

        value = str(value)

        value = value.replace(
            "\r\n",
            "\n",
        )

        value = value.replace(
            "\r",
            "\n",
        )

        return value.strip()

    # --------------------------------------------------------
    # Добавление ответа
    # --------------------------------------------------------

    def add_answer(value):

        value = clean_answer(value)

        if value:
            answers.append(value)

    # --------------------------------------------------------
    # ВАЖНО:
    #
    # find_all(True) возвращает элементы
    # в том порядке, в котором они идут
    # непосредственно в HTML.
    #
    # Мы больше НЕ собираем:
    #
    # сначала все test-item,
    # потом все input,
    # потом все select.
    #
    # Поэтому порядок не ломается.
    # --------------------------------------------------------

    for element in soup.find_all(True):

        tag = element.name.lower()

        # ====================================================
        # 1. math-input
        #
        # Это основной случай для обычных полей
        # математического ответа.
        #
        # Пример:
        #
        # <math-input id="MI1">
        #     <math-input-answer>20</math-input-answer>
        # </math-input>
        # ====================================================

        if tag == "math-input":

            answer = element.find(
                "math-input-answer",
                recursive=False,
            )

            if answer is None:
                answer = element.find(
                    "math-input-answer"
                )

            if answer is not None:

                value = answer.get_text(
                    " ",
                    strip=True,
                )

                add_answer(value)

            continue

        # ====================================================
        # 2. vim-input-item
        # ====================================================

        if tag == "vim-input-item":

            value = None

            for attr in (
                "value",
                "answer",
                "text",
                "correct-answer",
            ):

                attr_value = element.get(
                    attr
                )

                if attr_value:
                    value = attr_value
                    break

            if value is None:
                value = element.get_text(
                    " ",
                    strip=True,
                )

            add_answer(value)

            continue

        # ====================================================
        # 3. vim-test-item
        # ====================================================

        if tag == "vim-test-item":

            correct = element.get(
                "correct"
            )

            if correct is not None:

                correct_str = str(
                    correct
                ).lower()

                if correct_str in (
                    "true",
                    "1",
                    "yes",
                ):

                    add_answer(
                        element.get_text(
                            " ",
                            strip=True,
                        )
                    )

            continue

        # ====================================================
        # 4. vim-order-sentence-verify-item
        # ====================================================

        if tag == (
            "vim-order-sentence-verify-item"
        ):

            correct = element.get(
                "correct"
            )

            if correct is not None:

                correct_str = str(
                    correct
                ).lower()

                if correct_str in (
                    "true",
                    "1",
                    "yes",
                ):

                    add_answer(
                        element.get_text(
                            " ",
                            strip=True,
                        )
                    )

            continue

        # ====================================================
        # 5. vim-select-item
        # ====================================================

        if tag == "vim-select-item":

            correct = element.get(
                "correct"
            )

            if correct is not None:

                correct_str = str(
                    correct
                ).lower()

                if correct_str in (
                    "true",
                    "1",
                    "yes",
                ):

                    add_answer(
                        element.get_text(
                            " ",
                            strip=True,
                        )
                    )

            continue

        # ====================================================
        # 6. vim-test-image-item
        # ====================================================

        if tag == "vim-test-image-item":

            correct = element.get(
                "correct"
            )

            if correct is not None:

                correct_str = str(
                    correct
                ).lower()

                if correct_str in (
                    "true",
                    "1",
                    "yes",
                ):

                    add_answer(
                        element.get_text(
                            " ",
                            strip=True,
                        )
                    )

            continue

        # ====================================================
        # 7. vim-dnd-text-drop
        # ====================================================

        if tag == "vim-dnd-text-drop":

            drag_id = (
                element.get(
                    "answer-id"
                )
                or element.get(
                    "drag-id"
                )
                or element.get(
                    "answerId"
                )
            )

            if drag_id:

                drag = soup.find(
                    "vim-dnd-text-drag",
                    attrs={
                        "answer-id": drag_id
                    },
                )

                if drag is None:

                    drag = soup.find(
                        "vim-dnd-text-drag",
                        attrs={
                            "id": drag_id
                        },
                    )

                if drag is not None:

                    add_answer(
                        drag.get_text(
                            " ",
                            strip=True,
                        )
                    )

            continue

        # ====================================================
        # 8. vim-dnd-group-drag
        # ====================================================

        if tag == "vim-dnd-group-drag":

            drag_ids = element.get(
                "drag-ids"
            )

            if drag_ids:

                for drag_id in re.split(
                    r"[\s,;]+",
                    drag_ids.strip(),
                ):

                    if not drag_id:
                        continue

                    item = soup.find(
                        "vim-dnd-group-item",
                        attrs={
                            "id": drag_id
                        },
                    )

                    if item is not None:

                        add_answer(
                            item.get_text(
                                " ",
                                strip=True,
                            )
                        )

            continue

        # ====================================================
        # 9. vim-groups-row
        # ====================================================

        if tag == "vim-groups-row":

            items = element.find_all(
                "vim-groups-item"
            )

            for item in items:

                encoded = item.get(
                    "text"
                )

                if not encoded:

                    add_answer(
                        item.get_text(
                            " ",
                            strip=True,
                        )
                    )

                    continue

                try:

                    decoded = (
                        base64
                        .b64decode(
                            encoded
                        )
                        .decode(
                            "utf-8",
                            errors="ignore",
                        )
                    )

                    add_answer(
                        decoded
                    )

                except Exception:

                    add_answer(
                        encoded
                    )

            continue

        # ====================================================
        # 10. vim-strike-out-item
        # ====================================================

        if tag == "vim-strike-out-item":

            striked = element.get(
                "striked"
            )

            if striked is not None:

                striked_str = str(
                    striked
                ).lower()

                if striked_str in (
                    "true",
                    "1",
                    "yes",
                ):

                    add_answer(
                        element.get_text(
                            " ",
                            strip=True,
                        )
                    )

            continue

        # ====================================================
        # 11. vim-dnd-image-set-drop
        # ====================================================

        if tag == "vim-dnd-image-set-drop":

            drag_ids = (
                element.get(
                    "drag-ids"
                )
                or element.get(
                    "answer-id"
                )
            )

            if drag_ids:

                for drag_id in re.split(
                    r"[\s,;]+",
                    drag_ids.strip(),
                ):

                    if not drag_id:
                        continue

                    item = soup.find(
                        "vim-dnd-image-set-drag",
                        attrs={
                            "id": drag_id
                        },
                    )

                    if item is not None:

                        add_answer(
                            item.get_text(
                                " ",
                                strip=True,
                            )
                        )

            continue

        # ====================================================
        # 12. vim-dnd-image-drop
        # ====================================================

        if tag == "vim-dnd-image-drop":

            drag_id = (
                element.get(
                    "answer-id"
                )
                or element.get(
                    "drag-id"
                )
            )

            if drag_id:

                item = soup.find(
                    "vim-dnd-image-drag",
                    attrs={
                        "id": drag_id
                    },
                )

                if item is None:

                    item = soup.find(
                        "vim-dnd-image-drag",
                        attrs={
                            "answer-id": drag_id
                        },
                    )

                if item is not None:

                    add_answer(
                        item.get_text(
                            " ",
                            strip=True,
                        )
                    )

            continue

        # ====================================================
        # 13. edu-open-answer
        # ====================================================

        if tag == "edu-open-answer":

            element_id = element.get(
                "id"
            )

            if element_id == "OA1":

                add_answer(
                    "File upload required"
                )

            continue

    # ========================================================
    # ВОПРОС
    # ========================================================

    question = extract_task_question(
        soup
    )

    full_question = (
        extract_task_full_question(
            soup
        )
    )

    result = {
        "question": question,
        "full_question": full_question,
        "answers": answers,
        "task_number": task_number,
    }

    logger.info(
        "TASK %s ANSWERS COUNT: %s",
        task_number,
        len(answers),
    )

    logger.info(
        "TASK %s ANSWERS IN DOM ORDER: %s",
        task_number,
        answers,
    )

    return result


# ============================================================
# ПОЛУЧЕНИЕ ВСЕХ ЗАДАНИЙ
# ============================================================

async def load_all_tasks(
    room_name
):

    async with SkysmartAPIClient() as client:

        # ----------------------------------------------------
        # Авторизация
        # ----------------------------------------------------

        authenticated = (
            await client.authenticate()
        )

        if not authenticated:
            return []

        # ----------------------------------------------------
        # Получаем room
        # ----------------------------------------------------

        room_data = await client.get_room(
            room_name
        )

        if not room_data:
            return []

        # ----------------------------------------------------
        # Ищем UUID заданий
        # ----------------------------------------------------

        step_uuids = []

        meta = room_data.get(
            "meta"
        )

        if isinstance(meta, dict):

            uuids = meta.get(
                "stepUuids"
            )

            if isinstance(
                uuids,
                list,
            ):
                step_uuids = uuids

        # Иногда структура может быть
        # непосредственно в data.

        if not step_uuids:

            data = room_data.get(
                "data"
            )

            if isinstance(
                data,
                dict,
            ):

                uuids = data.get(
                    "stepUuids"
                )

                if isinstance(
                    uuids,
                    list,
                ):
                    step_uuids = uuids

        logger.info(
            "Найдено STEP UUID: %s",
            len(step_uuids),
        )

        if not step_uuids:
            return []

        # ----------------------------------------------------
        # Загружаем все задания параллельно
        # ----------------------------------------------------

        tasks = []

        for index, uuid in enumerate(
            step_uuids,
            start=1,
        ):

            tasks.append(
                client.get_task_html(
                    uuid
                )
            )

        contents = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        # ----------------------------------------------------
        # Парсим задания
        # ----------------------------------------------------

        result = []

        for index, content in enumerate(
            contents,
            start=1,
        ):

            if isinstance(
                content,
                Exception,
            ):

                logger.error(
                    "TASK %s ERROR: %s",
                    index,
                    content,
                )

                continue

            if not content:
                continue

            try:

                soup = BeautifulSoup(
                    content,
                    "html.parser",
                )

                task = (
                    extract_task_answer(
                        soup,
                        index,
                    )
                )

                result.append(
                    task
                )

            except Exception as e:

                logger.exception(
                    "Ошибка обработки задания %s: %s",
                    index,
                    e,
                )

        return result


# ============================================================
# СИНХРОННАЯ ОБЁРТКА
# ============================================================

def get_skysmart_answers(
    room_name
):

    try:

        return asyncio.run(
            load_all_tasks(
                room_name
            )
        )

    except Exception as e:

        logger.exception(
            "Ошибка get_skysmart_answers: %s",
            e,
        )

        return []


# ============================================================
# ФОРМИРОВАНИЕ МАССИВА ОТВЕТОВ
# ============================================================

def build_all_tasks_answers(
    data
):

    if not isinstance(
        data,
        list,
    ):
        return []

    all_tasks_answers = []

    for task in data:

        if not isinstance(
            task,
            dict,
        ):
            continue

        task_number = task.get(
            "task_number"
        )

        answers = task.get(
            "answers",
            [],
        )

        if not isinstance(
            answers,
            list,
        ):
            answers = []

        if not isinstance(
            task_number,
            int,
        ):
            continue

        while len(
            all_tasks_answers
        ) < task_number:

            all_tasks_answers.append(
                []
            )

        # НИКАКОЙ дедупликации здесь нет.
        #
        # Например:
        #
        # ["2", "2", "4", "3", "2", "4", "4"]
        #
        # останется именно таким.

        all_tasks_answers[
            task_number - 1
        ] = [
            clean_text(answer)
            for answer in answers
            if clean_text(answer)
        ]

    logger.info(
        "ALL_TASKS_ANSWERS: %s",
        all_tasks_answers,
    )

    return all_tasks_answers


# ============================================================
# ФОРМАТИРОВАНИЕ JSON
# ============================================================

def format_all_tasks_answers(
    all_tasks_answers
):

    # ВАЖНО:
    # indent=2 оставляет многострочный JSON.

    return json.dumps(
        all_tasks_answers,
        ensure_ascii=False,
        indent=2,
    )


# ============================================================
# TELEGRAM
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user:
        return

    user_id = user.id

    create_user_if_needed(
        user_id
    )

    if is_authorized(
        user_id
    ):

        user_states[
            user_id
        ] = "waiting_link"

        await update.message.reply_text(
            "Отправь ссылку на задание Skysmart.",
            reply_markup=main_keyboard(),
        )

        return

    user_states[
        user_id
    ] = "waiting_password"

    await update.message.reply_text(
        "Введите пароль для доступа.",
        reply_markup=password_keyboard(),
    )


# ============================================================
# ID
# ============================================================

async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user:
        return

    await update.message.reply_text(
        f"Ваш Telegram ID:\n{user.id}"
    )


# ============================================================
# KEYS
# ============================================================

async def keys_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user:
        return

    if user.id != OWNER_ID:

        await update.message.reply_text(
            "Нет доступа."
        )

        return

    try:

        result = (
            supabase
            .table("bot_passwords")
            .select("*")
            .eq("used", False)
            .execute()
        )

        if not result.data:

            await update.message.reply_text(
                "Свободных паролей нет."
            )

            return

        # ВНИМАНИЕ:
        # В таблице хранится hash, поэтому
        # старые пароли невозможно восстановить.
        #
        # Этот вывод оставлен для совместимости
        # с текущей логикой.

        hashes = []

        for row in result.data:

            if row.get(
                "password_hash"
            ):

                hashes.append(
                    row[
                        "password_hash"
                    ]
                )

        await update.message.reply_text(
            "\n".join(hashes)
        )

    except Exception as e:

        logger.exception(
            "Ошибка /keys: %s",
            e,
        )

        await update.message.reply_text(
            "Ошибка получения ключей."
        )


# ============================================================
# ОБРАБОТКА СООБЩЕНИЙ
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
    # ID
    # --------------------------------------------------------

    if text == "🆔 Мой ID":

        await update.message.reply_text(
            f"Ваш Telegram ID:\n{user_id}"
        )

        return

    # --------------------------------------------------------
    # Инициализация пользователя
    # --------------------------------------------------------

    create_user_if_needed(
        user_id
    )

    # --------------------------------------------------------
    # Если пользователь не авторизован
    # --------------------------------------------------------

    if not is_authorized(
        user_id
    ):

        # Пароль

        if use_password(
            user_id,
            text,
        ):

            user_states[
                user_id
            ] = "waiting_link"

            await update.message.reply_text(
                "Пароль принят.\n\n"
                "Теперь отправь ссылку на "
                "задание Skysmart.",
                reply_markup=main_keyboard(),
            )

            return

        await update.message.reply_text(
            "Неверный или уже использованный пароль."
        )

        return

    # --------------------------------------------------------
    # Кнопка получения ответов
    # --------------------------------------------------------

    if text == "📚 Получить ответы":

        user_states[
            user_id
        ] = "waiting_link"

        await update.message.reply_text(
            "Отправь ссылку на задание Skysmart."
        )

        return

    # --------------------------------------------------------
    # Ссылка Skysmart
    # --------------------------------------------------------

    room_name = extract_room_name(
        text
    )

    if not room_name:

        await update.message.reply_text(
            "Не удалось найти roomName в ссылке.\n\n"
            "Отправь ссылку вида:\n"
            "https://edu.skysmart.ru/student/..."
        )

        return

    # --------------------------------------------------------
    # Получаем задания
    # --------------------------------------------------------

    await update.message.reply_text(
        "⏳ Получаю задания и ответы с Skysmart..."
    )

    try:

        data = await asyncio.to_thread(
            get_skysmart_answers,
            room_name,
        )

        if not data:

            await update.message.reply_text(
                "Не удалось получить задания."
            )

            return

        # ----------------------------------------------------
        # Формируем итоговый массив
        # ----------------------------------------------------

        all_tasks_answers = (
            build_all_tasks_answers(
                data
            )
        )

        # ----------------------------------------------------
        # Формируем многострочный JSON
        # ----------------------------------------------------

        output = (
            format_all_tasks_answers(
                all_tasks_answers
            )
        )

        # ----------------------------------------------------
        # Telegram ограничивает сообщение
        # примерно 4096 символами.
        # ----------------------------------------------------

        MAX_MESSAGE_LENGTH = 3900

        if len(output) <= MAX_MESSAGE_LENGTH:

            await update.message.reply_text(
                output
            )

        else:

            for i in range(
                0,
                len(output),
                MAX_MESSAGE_LENGTH,
            ):

                chunk = output[
                    i:
                    i + MAX_MESSAGE_LENGTH
                ]

                await update.message.reply_text(
                    chunk
                )

        user_states[
            user_id
        ] = "waiting_link"

    except Exception as e:

        logger.exception(
            "Ошибка обработки ссылки: %s",
            e,
        )

        await update.message.reply_text(
            "Произошла ошибка при получении ответов."
        )


# ============================================================
# WEBHOOK
# ============================================================

telegram_app = None


async def telegram_webhook(
    request: Request
):

    global telegram_app

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            telegram_app.bot,
        )

        await telegram_app.process_update(
            update
        )

        return JSONResponse(
            {
                "ok": True
            }
        )

    except Exception as e:

        logger.exception(
            "Webhook error: %s",
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
# ROOT
# ============================================================

async def root(
    request: Request
):

    return PlainTextResponse(
        "Skysmart bot is running."
    )


# ============================================================
# HEALTH
# ============================================================

async def health(
    request: Request
):

    return JSONResponse(
        {
            "status": "ok"
        }
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
# ЗАПУСК
# ============================================================

async def initialize_telegram():

    global telegram_app

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

    await telegram_app.bot.set_webhook(
        webhook_url
    )

    logger.info(
        "Telegram webhook установлен: %s",
        webhook_url,
    )


async def startup():

    initialize_passwords()

    await initialize_telegram()


async def shutdown():

    global telegram_app

    if telegram_app:

        try:
            await telegram_app.bot.delete_webhook()
        except Exception:
            pass

        await telegram_app.stop()

        await telegram_app.shutdown()


@app.on_event("startup")
async def on_startup():

    await startup()


@app.on_event("shutdown")
async def on_shutdown():

    await shutdown()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Запуск приложения на порту %s",
        PORT,
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
    )
