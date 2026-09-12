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

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from supabase import create_client


# ============================================================
# ENV
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_URL = os.getenv("RENDER_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
OWNER_ID = os.getenv("OWNER_ID")
PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = "/telegram"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("skysmart_bot")


required_env = {
    "BOT_TOKEN": BOT_TOKEN,
    "RENDER_URL": RENDER_URL,
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_SERVICE_KEY": SUPABASE_SERVICE_KEY,
    "OWNER_ID": OWNER_ID,
}

missing_env = [
    name
    for name, value in required_env.items()
    if not value
]

if missing_env:
    raise RuntimeError(
        "Не заданы переменные окружения: "
        + ", ".join(missing_env)
    )


supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY
)


user_states = {}


PASSWORD_ALPHABET = (
    "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
)


# ============================================================
# SKYSMART API
# ============================================================

SKYSMART_ROOM_URL = (
    "https://api-edu.skysmart.ru"
    "/api/v1/task/preview"
)

SKYSMART_AUTH_URL = (
    "https://api-edu.skysmart.ru"
    "/api/v1/user/registration/teacher"
)

SKYSMART_STEP_URL = (
    "https://api-edu.skysmart.ru"
    "/api/v1/content/step/load?stepUuid="
)


class SkysmartAPIClient:

    def __init__(self):

        self.session = aiohttp.ClientSession()

        self.token = ""

        self.user_agent = (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36"
        )

    async def close(self):

        if not self.session.closed:
            await self.session.close()

    async def _authenticate(self):

        headers = {
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

        logger.info(
            "Skysmart: выполняю авторизацию"
        )

        async with self.session.post(
            SKYSMART_AUTH_URL,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=60
            ),
        ) as response:

            if response.status != 200:

                text = await response.text()

                raise RuntimeError(
                    "Skysmart authentication failed: "
                    f"HTTP {response.status} "
                    f"{text[:1000]}"
                )

            data = await response.json()

            self.token = data.get(
                "jwtToken",
                ""
            )

            if not self.token:
                raise RuntimeError(
                    "Skysmart не вернул jwtToken."
                )

        logger.info(
            "Skysmart: JWT получен"
        )

    async def _get_headers(self):

        if not self.token:
            await self._authenticate()

        return {
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
            "Accept": (
                "application/json, "
                "text/plain, */*"
            ),
            "Authorization": (
                f"Bearer {self.token}"
            ),
        }

    async def get_room(
        self,
        task_hash
    ):

        headers = await self._get_headers()

        payload = {
            "taskHash": task_hash
        }

        logger.info(
            "Skysmart: получаю room "
            "taskHash=%s",
            task_hash
        )

        async with self.session.post(
            SKYSMART_ROOM_URL,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(
                total=60
            ),
        ) as response:

            if response.status != 200:

                text = await response.text()

                raise RuntimeError(
                    "get_room failed: "
                    f"HTTP {response.status} "
                    f"{text[:2000]}"
                )

            data = await response.json()

        step_uuids = (
            data
            .get("meta", {})
            .get("stepUuids", [])
        )

        logger.info(
            "Skysmart: найдено заданий: %s",
            len(step_uuids)
        )

        return step_uuids

    async def get_task_html(
        self,
        uuid
    ):

        headers = await self._get_headers()

        url = (
            SKYSMART_STEP_URL
            + str(uuid)
        )

        async with self.session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=60
            ),
        ) as response:

            if response.status != 200:

                text = await response.text()

                raise RuntimeError(
                    "get_task_html failed: "
                    f"HTTP {response.status} "
                    f"{text[:1000]}"
                )

            data = await response.json()

        return data.get(
            "content",
            ""
        )


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(text):

    if not text:
        return ""

    text = (
        str(text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    lines = []

    for line in text.split("\n"):

        line = line.strip()

        if line:
            lines.append(line)

    return "\n".join(lines).strip()


def remove_extra_newlines(text):

    return re.sub(
        r"\n+",
        "\n",
        text.strip()
    )


# ============================================================
# SKYSMART QUESTION PARSER
# ============================================================

def extract_task_question(soup):

    instruction = soup.find(
        "vim-instruction"
    )

    if not instruction:
        return ""

    return clean_text(
        instruction.get_text(
            " ",
            strip=True
        )
    )


def extract_task_full_question(soup):

    soup_copy = BeautifulSoup(
        str(soup),
        "html.parser"
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

    for element in soup_copy.find_all(
        elements_to_exclude
    ):
        element.decompose()

    text = soup_copy.get_text(
        "\n",
        strip=True
    )

    return remove_extra_newlines(
        text
    )


# ============================================================
# SKYSMART ANSWER PARSER
# ============================================================

def extract_task_answer(
    soup,
    task_number
):

    # ========================================================
    # Здесь храним все найденные ответы.
    #
    # ВАЖНО:
    # одинаковые ответы НЕ удаляются.
    #
    # Например:
    #
    # ["2", "2", "3", "2"]
    #
    # останется именно:
    #
    # ["2", "2", "3", "2"]
    # ========================================================

    ordered_answers = []

    soup_html = str(soup)

    # --------------------------------------------------------
    # Получаем позицию элемента в исходном HTML
    # --------------------------------------------------------

    def get_position(element):

        try:

            element_html = str(element)

            position = soup_html.find(
                element_html
            )

            if position >= 0:
                return position

        except Exception:
            pass

        return 999999999

    # --------------------------------------------------------
    # Добавляем ответ
    # --------------------------------------------------------

    def add_answer(
        element,
        text,
        priority=0
    ):

        text = clean_text(text)

        if not text:
            return

        ordered_answers.append(
            {
                "position": get_position(
                    element
                ),
                "priority": priority,
                "order": len(
                    ordered_answers
                ),
                "text": text,
            }
        )

    # ========================================================
    # 1. Обычный тест
    # ========================================================

    for item in soup.find_all(
        "vim-test-item"
    ):

        correct = str(
            item.get(
                "correct",
                ""
            )
        ).lower()

        if correct != "true":
            continue

        text = item.get_text(
            " ",
            strip=True
        )

        add_answer(
            item,
            text,
            1
        )

    # ========================================================
    # 2. Упорядочивание предложений
    # ========================================================

    for item in soup.find_all(
        "vim-order-sentence-verify-item"
    ):

        text = item.get_text(
            " ",
            strip=True
        )

        add_answer(
            item,
            text,
            2
        )

    # ========================================================
    # 3. Поля ввода
    #
    # В старой версии использовался find(),
    # поэтому бралось только первое поле.
    #
    # Теперь обрабатываем ВСЕ vim-input-item.
    # ========================================================

    for input_answer in soup.find_all(
        "vim-input-answers"
    ):

        input_items = input_answer.find_all(
            "vim-input-item"
        )

        for input_item in input_items:

            text = input_item.get_text(
                " ",
                strip=True
            )

            if not text:

                for attr in (
                    "value",
                    "answer",
                    "text"
                ):

                    value = input_item.get(
                        attr
                    )

                    if value:

                        text = str(
                            value
                        )

                        break

            add_answer(
                input_item,
                text,
                3
            )

    # ========================================================
    # 4. Select
    # ========================================================

    for item in soup.find_all(
        "vim-select-item"
    ):

        correct = str(
            item.get(
                "correct",
                ""
            )
        ).lower()

        if correct != "true":
            continue

        text = item.get_text(
            " ",
            strip=True
        )

        add_answer(
            item,
            text,
            4
        )

    # ========================================================
    # 5. Выбор изображения
    # ========================================================

    for item in soup.find_all(
        "vim-test-image-item"
    ):

        correct = str(
            item.get(
                "correct",
                ""
            )
        ).lower()

        if correct != "true":
            continue

        text = item.get_text(
            " ",
            strip=True
        )

        if text:

            text = (
                f"{text} - Correct"
            )

        add_answer(
            item,
            text,
            5
        )

    # ========================================================
    # 6. Математический ответ
    # ========================================================

    for item in soup.find_all(
        "math-input-answer"
    ):

        text = item.get_text(
            " ",
            strip=True
        )

        if not text:

            for attr in (
                "value",
                "answer",
                "text"
            ):

                value = item.get(
                    attr
                )

                if value:

                    text = str(
                        value
                    )

                    break

        add_answer(
            item,
            text,
            6
        )

    # ========================================================
    # 7. Drag & Drop текста
    # ========================================================

    for drop in soup.find_all(
        "vim-dnd-text-drop"
    ):

        drag_ids = [
            x.strip()
            for x in drop.get(
                "drag-ids",
                ""
            ).split(",")
            if x.strip()
        ]

        for drag_id in drag_ids:

            drag = soup.find(
                "vim-dnd-text-drag",
                attrs={
                    "answer-id": drag_id
                }
            )

            if not drag:
                continue

            text = drag.get_text(
                " ",
                strip=True
            )

            add_answer(
                drop,
                text,
                7
            )

    # ========================================================
    # 8. Drag & Drop по группам
    # ========================================================

    for drag_group in soup.find_all(
        "vim-dnd-group-drag"
    ):

        answer_id = drag_group.get(
            "answer-id"
        )

        if not answer_id:
            continue

        drag_text = clean_text(
            drag_group.get_text(
                " ",
                strip=True
            )
        )

        for group_item in soup.find_all(
            "vim-dnd-group-item"
        ):

            drag_ids = [
                x.strip()
                for x in group_item.get(
                    "drag-ids",
                    ""
                ).split(",")
                if x.strip()
            ]

            if answer_id not in drag_ids:
                continue

            group_text = clean_text(
                group_item.get_text(
                    " ",
                    strip=True
                )
            )

            if group_text and drag_text:

                text = (
                    f"{group_text} - "
                    f"{drag_text}"
                )

            elif group_text:

                text = group_text

            else:

                text = drag_text

            add_answer(
                drag_group,
                text,
                8
            )

    # ========================================================
    # 9. Base64 группы
    # ========================================================

    for group_row in soup.find_all(
        "vim-groups-row"
    ):

        for group_item in group_row.find_all(
            "vim-groups-item"
        ):

            encoded_text = group_item.get(
                "text"
            )

            if not encoded_text:
                continue

            try:

                decoded_text = (
                    base64.b64decode(
                        encoded_text
                    )
                    .decode(
                        "utf-8"
                    )
                )

                decoded_text = clean_text(
                    decoded_text
                )

                add_answer(
                    group_item,
                    decoded_text,
                    9
                )

            except Exception as e:

                logger.warning(
                    "Ошибка Base64: %s",
                    e
                )

    # ========================================================
    # 10. Зачёркивание
    # ========================================================

    for item in soup.find_all(
        "vim-strike-out-item"
    ):

        striked = str(
            item.get(
                "striked",
                ""
            )
        ).lower()

        if striked != "true":
            continue

        text = item.get_text(
            " ",
            strip=True
        )

        add_answer(
            item,
            text,
            10
        )

    # ========================================================
    # 11. Drag & Drop изображений
    # ========================================================

    for image_drag in soup.find_all(
        "vim-dnd-image-set-drag"
    ):

        answer_id = image_drag.get(
            "answer-id"
        )

        if not answer_id:
            continue

        drag_text = clean_text(
            image_drag.get_text(
                " ",
                strip=True
            )
        )

        for image_drop in soup.find_all(
            "vim-dnd-image-set-drop"
        ):

            drag_ids = [
                x.strip()
                for x in image_drop.get(
                    "drag-ids",
                    ""
                ).split(",")
                if x.strip()
            ]

            if answer_id not in drag_ids:
                continue

            image = clean_text(
                image_drop.get(
                    "image",
                    ""
                )
            )

            if image and drag_text:

                text = (
                    f"{image} - "
                    f"{drag_text}"
                )

            elif drag_text:

                text = drag_text

            else:

                text = image

            add_answer(
                image_drag,
                text,
                11
            )

    # ========================================================
    # 12. Обычный Drag & Drop изображений
    # ========================================================

    for image_drag in soup.find_all(
        "vim-dnd-image-drag"
    ):

        answer_id = image_drag.get(
            "answer-id"
        )

        if not answer_id:
            continue

        drag_text = clean_text(
            image_drag.get_text(
                " ",
                strip=True
            )
        )

        for image_drop in soup.find_all(
            "vim-dnd-image-drop"
        ):

            drag_ids = [
                x.strip()
                for x in image_drop.get(
                    "drag-ids",
                    ""
                ).split(",")
                if x.strip()
            ]

            if answer_id not in drag_ids:
                continue

            drop_text = clean_text(
                image_drop.get_text(
                    " ",
                    strip=True
                )
            )

            if drop_text and drag_text:

                text = (
                    f"{drop_text} - "
                    f"{drag_text}"
                )

            elif drag_text:

                text = drag_text

            else:

                text = drop_text

            add_answer(
                image_drag,
                text,
                12
            )

    # ========================================================
    # 13. Открытый ответ
    # ========================================================

    open_answer = soup.find(
        "edu-open-answer",
        attrs={
            "id": "OA1"
        }
    )

    if open_answer:

        add_answer(
            open_answer,
            "File upload required",
            13
        )

    # ========================================================
    # Сортируем ответы по их позиции в HTML
    # ========================================================

    ordered_answers.sort(
        key=lambda item: (
            item["position"],
            item["priority"],
            item["order"]
        )
    )

    # ========================================================
    # НЕ УДАЛЯЕМ ДУБЛИКАТЫ
    # ========================================================

    answers = [
        item["text"]
        for item in ordered_answers
    ]

    logger.info(
        "Skysmart: задание #%s -> %s ответов",
        task_number,
        len(answers)
    )

    logger.info(
        "Skysmart: ответы задания #%s: %s",
        task_number,
        answers
    )

    return {
        "question": extract_task_question(
            soup
        ),

        "full_question":
            extract_task_full_question(
                soup
            ),

        "answers": answers,

        "task_number": task_number,
    }


# ============================================================
# GET SKYSMART ANSWERS
# ============================================================

def get_skysmart_answers(room_name):

    async def load():

        client = SkysmartAPIClient()

        try:

            logger.info(
                "Skysmart: начинаю получение "
                "комнаты %s",
                room_name
            )

            step_uuids = await client.get_room(
                room_name
            )

            if not step_uuids:
                raise RuntimeError(
                    "Skysmart не вернул задания."
                )

            logger.info(
                "Skysmart: UUID заданий: %s",
                step_uuids
            )

            # Загружаем все задания параллельно
            coroutines = [
                client.get_task_html(uuid)
                for uuid in step_uuids
            ]

            html_list = await asyncio.gather(
                *coroutines,
                return_exceptions=True
            )

            result = []

            for index, html in enumerate(
                html_list,
                start=1
            ):

                if isinstance(
                    html,
                    Exception
                ):

                    logger.error(
                        "Ошибка задания #%s: %s",
                        index,
                        html
                    )

                    continue

                if not html:

                    logger.warning(
                        "Пустой HTML задания #%s",
                        index
                    )

                    continue

                try:

                    soup = BeautifulSoup(
                        html,
                        "html.parser"
                    )

                    task = extract_task_answer(
                        soup,
                        index
                    )

                    result.append(
                        task
                    )

                    logger.info(
                        "Skysmart: задание #%s "
                        "-> %s ответов",
                        index,
                        len(
                            task["answers"]
                        )
                    )

                except Exception as e:

                    logger.exception(
                        "Ошибка обработки "
                        "задания #%s: %s",
                        index,
                        e
                    )

            return result

        finally:

            await client.close()

    return asyncio.run(load())


# ============================================================
# AUTH / OLD FUNCTIONS
# ============================================================

def password_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "🆔 Мой ID"
                )
            ]
        ],
        resize_keyboard=True,
    )


def main_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📚 Получить ответы"
                )
            ],
            [
                KeyboardButton(
                    "🆔 Мой ID"
                )
            ],
        ],
        resize_keyboard=True,
    )


def generate_password():

    part1 = "".join(
        secrets.choice(
            PASSWORD_ALPHABET
        )
        for _ in range(4)
    )

    part2 = "".join(
        secrets.choice(
            PASSWORD_ALPHABET
        )
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

            try:

                (
                    supabase
                    .table("bot_passwords")
                    .insert(
                        {
                            "password_hash":
                                password_hash,
                            "password_text":
                                password,
                            "used":
                                False,
                        }
                    )
                    .execute()
                )

                current_count += 1

            except Exception as e:

                logger.error(
                    "Ошибка создания пароля: %s",
                    e
                )

    except Exception as e:

        logger.error(
            "Ошибка initialize_passwords: %s",
            e
        )


def create_user_if_needed(
    telegram_id
):

    try:

        result = (
            supabase
            .table("bot_users")
            .select("*")
            .eq(
                "telegram_id",
                telegram_id
            )
            .execute()
        )

        if result.data:
            return result.data[0]

        result = (
            supabase
            .table("bot_users")
            .insert(
                {
                    "telegram_id":
                        telegram_id,
                    "authorized":
                        False,
                }
            )
            .execute()
        )

        if result.data:
            return result.data[0]

    except Exception as e:

        logger.error(
            "Ошибка create_user_if_needed: %s",
            e
        )

    return None


def is_authorized(
    telegram_id
):

    try:

        result = (
            supabase
            .table("bot_users")
            .select("authorized")
            .eq(
                "telegram_id",
                telegram_id
            )
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

        logger.error(
            "Ошибка is_authorized: %s",
            e
        )

        return False


def use_password(
    password,
    telegram_id
):

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
            .execute()
        )

        if not result.data:
            return False

        password_row = result.data[0]

        password_id = password_row["id"]

        (
            supabase
            .table("bot_passwords")
            .update(
                {
                    "used":
                        True,
                    "used_by":
                        telegram_id,
                    "used_at":
                        datetime.now(
                            timezone.utc
                        ).isoformat(),
                }
            )
            .eq(
                "id",
                password_id
            )
            .execute()
        )

        (
            supabase
            .table("bot_users")
            .upsert(
                {
                    "telegram_id":
                        telegram_id,
                    "authorized":
                        True,
                }
            )
            .execute()
        )

        return True

    except Exception as e:

        logger.error(
            "Ошибка use_password: %s",
            e
        )

        return False


def make_password_list():

    try:

        result = (
            supabase
            .table("bot_passwords")
            .select("password_text")
            .eq(
                "used",
                False
            )
            .order("id")
            .execute()
        )

        passwords = [
            row["password_text"]
            for row in (
                result.data or []
            )
        ]

        if not passwords:
            return "Свободных паролей нет."

        return "\n".join(
            f"`{password}`"
            for password in passwords
        )

    except Exception as e:

        logger.error(
            "Ошибка make_password_list: %s",
            e
        )

        return "Ошибка получения паролей."


def extract_room_name(text):

    text = text.strip()

    match = re.search(
        r"edu\.skysmart\.ru/student/([^/?#\s]+)",
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    if re.fullmatch(
        r"[A-Za-z0-9_-]+",
        text
    ):
        return text

    return None


# ============================================================
# BUILD ANSWERS
# ============================================================

def build_all_tasks_answers(data):

    if not isinstance(
        data,
        list
    ):

        raise ValueError(
            "Ответ Skysmart должен "
            "быть списком."
        )

    if len(data) == 0:

        raise ValueError(
            "Skysmart вернул пустой список."
        )

    all_tasks_answers = []

    for task in data:

        if not isinstance(
            task,
            dict
        ):
            continue

        task_number = task.get(
            "task_number"
        )

        answers = task.get(
            "answers",
            []
        )

        if not isinstance(
            task_number,
            int
        ):
            continue

        index = task_number - 1

        while len(
            all_tasks_answers
        ) <= index:

            all_tasks_answers.append([])

        if isinstance(
            answers,
            list
        ):

            # ВАЖНО:
            # сохраняем все ответы как есть,
            # включая одинаковые.
            all_tasks_answers[
                index
            ] = list(answers)

        elif isinstance(
            answers,
            str
        ):

            all_tasks_answers[
                index
            ] = [
                answers
            ]

        else:

            all_tasks_answers[
                index
            ] = [
                answers
            ]

    logger.info(
        "ALL_TASKS_ANSWERS = %s",
        all_tasks_answers
    )

    return all_tasks_answers


# ============================================================
# МНОГОСТРОЧНЫЙ JSON
# ============================================================

def format_all_tasks_answers(
    all_tasks_answers
):

    # Оставляем многострочный формат.
    # Никакого вывода в одну строку.

    return json.dumps(
        all_tasks_answers,
        ensure_ascii=False,
        indent=2,
    )


# ============================================================
# TELEGRAM HANDLERS
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    telegram_id = (
        update.effective_user.id
    )

    create_user_if_needed(
        telegram_id
    )

    if is_authorized(
        telegram_id
    ):

        user_states[
            telegram_id
        ] = "waiting_link"

        await update.message.reply_text(
            "✅ Вы уже авторизованы.\n\n"
            "Отправьте ссылку на задание Skysmart:\n\n"
            "https://edu.skysmart.ru/student/ROOM",
            reply_markup=main_keyboard(),
        )

        return

    user_states[
        telegram_id
    ] = "waiting_password"

    await update.message.reply_text(
        "🔐 Для использования бота "
        "введите пароль доступа.",
        reply_markup=password_keyboard(),
    )


async def id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    telegram_id = (
        update.effective_user.id
    )

    await update.message.reply_text(
        "🆔 Ваш Telegram ID:\n\n"
        f"{telegram_id}"
    )


async def keys_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    telegram_id = str(
        update.effective_user.id
    )

    if telegram_id != str(
        OWNER_ID
    ):

        await update.message.reply_text(
            "❌ У вас нет доступа."
        )

        return

    passwords = make_password_list()

    await update.message.reply_text(
        "🔑 Свободные пароли:\n\n"
        + passwords
    )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_user:
        return

    if not update.message:
        return

    telegram_id = (
        update.effective_user.id
    )

    text = (
        update.message.text or ""
    ).strip()

    # --------------------------------------------------------
    # ID
    # --------------------------------------------------------

    if text == "🆔 Мой ID":

        await id_command(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # Получить ответы
    # --------------------------------------------------------

    if text == "📚 Получить ответы":

        user_states[
            telegram_id
        ] = "waiting_link"

        await update.message.reply_text(
            "Отправьте ссылку на задание Skysmart."
        )

        return

    # --------------------------------------------------------
    # Авторизация
    # --------------------------------------------------------

    if not is_authorized(
        telegram_id
    ):

        if (
            user_states.get(
                telegram_id
            )
            == "waiting_password"
        ):

            if use_password(
                text,
                telegram_id
            ):

                user_states[
                    telegram_id
                ] = "waiting_link"

                await update.message.reply_text(
                    "✅ Пароль принят!\n\n"
                    "Теперь отправьте ссылку "
                    "на задание Skysmart.",
                    reply_markup=main_keyboard(),
                )

            else:

                await update.message.reply_text(
                    "❌ Неверный или уже "
                    "использованный пароль.\n\n"
                    "Попробуйте ещё раз."
                )

            return

        user_states[
            telegram_id
        ] = "waiting_password"

        await update.message.reply_text(
            "🔐 Сначала введите пароль доступа."
        )

        return

    # --------------------------------------------------------
    # Получаем room
    # --------------------------------------------------------

    room_name = extract_room_name(
        text
    )

    if not room_name:

        await update.message.reply_text(
            "❌ Не удалось найти roomName.\n\n"
            "Отправьте ссылку вида:\n"
            "https://edu.skysmart.ru/student/ROOM"
        )

        return

    processing_message = (
        await update.message.reply_text(
            "⏳ Получаю задания и ответы "
            "с Skysmart...",
            parse_mode=None,
        )
    )

    try:

        data = await asyncio.to_thread(
            get_skysmart_answers,
            room_name
        )

        all_tasks_answers = (
            build_all_tasks_answers(
                data
            )
        )

        result_text = (
            format_all_tasks_answers(
                all_tasks_answers
            )
        )

        logger.info(
            "Готовый ALL_TASKS_ANSWERS:\n%s",
            result_text
        )

        try:

            await processing_message.delete()

        except Exception:
            pass

        max_length = 3900

        if len(result_text) <= max_length:

            await update.message.reply_text(
                result_text,
                parse_mode=None,
            )

        else:

            for i in range(
                0,
                len(result_text),
                max_length
            ):

                chunk = result_text[
                    i:i + max_length
                ]

                await update.message.reply_text(
                    chunk,
                    parse_mode=None,
                )

    except Exception as e:

        logger.exception(
            "Ошибка обработки Skysmart: %s",
            e
        )

        try:

            await processing_message.delete()

        except Exception:
            pass

        await update.message.reply_text(
            "❌ Произошла ошибка "
            "при получении заданий.\n\n"
            f"{str(e)[:1000]}",
            parse_mode=None,
        )


# ============================================================
# HTTP
# ============================================================

async def health(request):

    return JSONResponse(
        {
            "status": "ok"
        }
    )


async def root(request):

    return PlainTextResponse(
        "Bot is running."
    )


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

        return JSONResponse(
            {
                "ok": True
            }
        )

    except Exception as e:

        logger.exception(
            "Ошибка webhook: %s",
            e
        )

        return JSONResponse(
            {
                "ok": False,
                "error": str(e),
            },
            status_code=500,
        )


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

application = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


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
        text_handler
    )
)


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

async def startup():

    initialize_passwords()

    await application.initialize()

    await application.start()

    webhook_url = (
        RENDER_URL.rstrip("/")
        + WEBHOOK_PATH
    )

    logger.info(
        "Устанавливаем webhook: %s",
        webhook_url
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    logger.info(
        "Webhook установлен."
    )


async def shutdown():

    await application.stop()

    await application.shutdown()


# ============================================================
# STARLETTE
# ============================================================

app = Starlette(
    routes=[
        Route(
            "/",
            root,
            methods=["GET"]
        ),
        Route(
            "/health",
            health,
            methods=["GET"]
        ),
        Route(
            WEBHOOK_PATH,
            telegram_webhook,
            methods=["POST"]
        ),
    ],
    on_startup=[
        startup
    ],
    on_shutdown=[
        shutdown
    ],
)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
    )
