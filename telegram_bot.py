#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from main import (
    DEFAULT_BASE_URL,
    DEFAULT_CONTENT_BASE_URL,
    DEFAULT_PAGE_SIZE,
    DEFAULT_REQUEST_PAUSE,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    FetchOptions,
    WBAPIError,
    WBClient,
    _write_csv,
)


ACTION_REVIEWS = "reviews"
ACTION_QUESTIONS = "questions"
ACTION_TITLES = {
    ACTION_REVIEWS: "Отзывы",
    ACTION_QUESTIONS: "Вопросы",
}

STATE_AWAITING_TOKEN = "awaiting_token"
STATE_PRODUCTS = "products"
STATE_PRODUCTS_PAGE = "products_page"
STATE_PRODUCTS_LIMIT_HIT = "products_limit_hit"
STATE_PRODUCTS_CACHED_AT = "products_cached_at"

CALLBACK_SET_TOKEN = "set_token"
CALLBACK_DELETE_TOKEN = "delete_token"
CALLBACK_REFRESH_PRODUCTS = "refresh_products"
CALLBACK_BACK_TO_PRODUCTS = "back_to_products"
CALLBACK_NOOP = "noop"
CALLBACK_PAGE_PREFIX = "products_page:"
CALLBACK_SELECT_PREFIX = "select_product:"
CALLBACK_DOWNLOAD_PREFIX = "download:"

UI_PRODUCTS_PER_PAGE = 8
MAX_REVIEWS_EXPORT = 500
CACHE_TTL_SECONDS = 600


class UserTokenStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._tokens = self._load()

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logging.warning("Не удалось прочитать хранилище токенов: %s", self._path)
            return {}

        if not isinstance(payload, dict):
            return {}
        return {
            str(user_id): str(token)
            for user_id, token in payload.items()
            if isinstance(token, str) and token.strip()
        }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._tokens, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    async def has_token(self, user_id: int) -> bool:
        async with self._lock:
            return str(user_id) in self._tokens

    async def get_token(self, user_id: int) -> str | None:
        async with self._lock:
            return self._tokens.get(str(user_id))

    async def set_token(self, user_id: int, token: str) -> None:
        async with self._lock:
            self._tokens[str(user_id)] = token
            self._save()

    async def delete_token(self, user_id: int) -> bool:
        async with self._lock:
            removed = self._tokens.pop(str(user_id), None)
            if removed is not None:
                self._save()
            return removed is not None


def _is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


def _user_id(update: Update) -> int | None:
    user = update.effective_user
    if user is None:
        return None
    return user.id


def _extract_token(raw_text: str) -> str:
    token = raw_text.strip().strip('"').strip("'")
    if "=" in token:
        left, right = token.split("=", 1)
        if left.strip().upper() in {"WB_API_TOKEN", "B_API_TOKEN", "TOKEN", "API_TOKEN"}:
            token = right.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _looks_like_wb_token(token: str) -> bool:
    return token.count(".") == 2 and len(token) >= 80


def _token_keyboard(has_token: bool) -> InlineKeyboardMarkup:
    if has_token:
        rows = [
            [InlineKeyboardButton("🔄 Обновить список товаров", callback_data=CALLBACK_REFRESH_PRODUCTS)],
            [InlineKeyboardButton("🔑 Обновить WB токен", callback_data=CALLBACK_SET_TOKEN)],
            [InlineKeyboardButton("🗑 Удалить WB токен", callback_data=CALLBACK_DELETE_TOKEN)],
        ]
    else:
        rows = [[InlineKeyboardButton("🔑 Подключить WB токен", callback_data=CALLBACK_SET_TOKEN)]]
    return InlineKeyboardMarkup(rows)


def _truncate(text: str, max_len: int = 36) -> str:
    clean = " ".join(text.split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def _append_photo_version(url: str, card: dict[str, object]) -> str:
    version_raw = (
        card.get("updatedAt")
        or card.get("updateAt")
        or card.get("modifiedAt")
        or card.get("createdAt")
    )
    if version_raw is None:
        return url

    version = str(version_raw).strip()
    if not version:
        return url

    split = urlsplit(url)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    query["wbv"] = version
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


def _extract_photo_url(card: dict[str, object]) -> str | None:
    def _collect(items: object, base_score: int) -> list[tuple[int, int, int, int, str]]:
        candidates: list[tuple[int, int, int, int, str]] = []
        if not isinstance(items, list):
            return candidates

        key_scores = {
            "big": 60,
            "c516x688": 55,
            "c246x328": 50,
            "tm": 45,
            "url": 40,
        }
        for idx, item in enumerate(items):
            if isinstance(item, str) and item.startswith("http"):
                # Prefer the first photo in list (index 0).
                candidates.append((base_score, 0, 0, -idx, item))
                continue
            if isinstance(item, dict):
                is_main = 1 if item.get("isMain") is True else 0
                for key, key_score in key_scores.items():
                    value = item.get(key)
                    if isinstance(value, str) and value.startswith("http"):
                        # Priority: source -> isMain -> quality -> first index.
                        candidates.append((base_score, is_main, key_score, -idx, value))
        return candidates

    all_candidates: list[tuple[int, int, int, int, str]] = []
    for key, base_score in (("photos", 30), ("mediaFiles", 20), ("images", 10)):
        all_candidates.extend(_collect(card.get(key), base_score=base_score))

    best_url: str | None = None
    if all_candidates:
        _, _, _, _, best_url = max(all_candidates, key=lambda item: item[:4])
        return _append_photo_version(best_url, card)

    for key in ("photo", "image", "imageUrl"):
        value = card.get(key)
        if isinstance(value, str) and value.startswith("http"):
            best_url = value
            break
    if best_url:
        return _append_photo_version(best_url, card)
    return None


def _normalize_products(cards: list[dict[str, object]]) -> list[dict[str, object]]:
    products: list[dict[str, object]] = []
    seen_nm_ids: set[int] = set()

    for card in cards:
        nm_raw = card.get("nmID")
        if nm_raw is None:
            nm_raw = card.get("nmId")
        try:
            nm_id = int(nm_raw)
        except (TypeError, ValueError):
            continue
        if nm_id in seen_nm_ids:
            continue
        seen_nm_ids.add(nm_id)

        title = str(card.get("title") or card.get("subjectName") or "").strip() or "Без названия"
        vendor_code = str(card.get("vendorCode") or "-").strip() or "-"
        products.append(
            {
                "nm_id": nm_id,
                "title": title,
                "vendor_code": vendor_code,
                "photo_url": _extract_photo_url(card),
            }
        )

    products.sort(key=lambda item: (str(item["title"]).lower(), int(item["nm_id"])))
    return products


def _products_text(products: list[dict[str, object]], page: int, hit_limit: bool) -> str:
    total = len(products)
    if total == 0:
        text = (
            "Товары не найдены.\n\n"
            "Проверь, что у токена есть доступ к API товаров и у кабинета есть карточки."
        )
        if hit_limit:
            text += "\n\nПоказан неполный список (достигнут лимит загрузки)."
        return text

    total_pages = max(1, (total + UI_PRODUCTS_PER_PAGE - 1) // UI_PRODUCTS_PER_PAGE)
    start_idx = page * UI_PRODUCTS_PER_PAGE + 1
    end_idx = min((page + 1) * UI_PRODUCTS_PER_PAGE, total)

    text = (
        f"Товары WB: {total}\n"
        f"Страница: {page + 1}/{total_pages} ({start_idx}-{end_idx})\n\n"
        "Выбери товар по кнопке."
    )
    if hit_limit:
        text += "\n\nВнимание: показан неполный список (достигнут лимит загрузки)."
    return text


def _products_keyboard(products: list[dict[str, object]], page: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    total = len(products)
    total_pages = max(1, (total + UI_PRODUCTS_PER_PAGE - 1) // UI_PRODUCTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * UI_PRODUCTS_PER_PAGE
    end = min(start + UI_PRODUCTS_PER_PAGE, total)
    for product in products[start:end]:
        nm_id = int(product["nm_id"])
        title = str(product["title"])
        label = f"WB {nm_id} · {_truncate(title)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{CALLBACK_SELECT_PREFIX}{nm_id}")])

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"{CALLBACK_PAGE_PREFIX}{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=CALLBACK_NOOP))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"{CALLBACK_PAGE_PREFIX}{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton("🔄 Обновить список товаров", callback_data=CALLBACK_REFRESH_PRODUCTS)])
    rows.append([InlineKeyboardButton("🔑 Обновить WB токен", callback_data=CALLBACK_SET_TOKEN)])
    rows.append([InlineKeyboardButton("🗑 Удалить WB токен", callback_data=CALLBACK_DELETE_TOKEN)])
    return InlineKeyboardMarkup(rows)


def _product_details_text(product: dict[str, object]) -> str:
    title = str(product["title"])
    nm_id = int(product["nm_id"])
    vendor_code = str(product["vendor_code"])
    return (
        "📦 Карточка товара\n"
        f"• Название: {title}\n"
        f"• Артикул WB: {nm_id}\n"
        f"• Артикул продавца: {vendor_code}\n\n"
        "Выберите выгрузку:"
    )


def _product_actions_keyboard(nm_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Скачать отзывы CSV",
                    callback_data=f"{CALLBACK_DOWNLOAD_PREFIX}{ACTION_REVIEWS}:{nm_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "Скачать вопросы CSV",
                    callback_data=f"{CALLBACK_DOWNLOAD_PREFIX}{ACTION_QUESTIONS}:{nm_id}",
                )
            ],
            [InlineKeyboardButton("⬅️ К списку товаров", callback_data=CALLBACK_BACK_TO_PRODUCTS)],
            [InlineKeyboardButton("🔑 Обновить WB токен", callback_data=CALLBACK_SET_TOKEN)],
        ]
    )


def _find_product(products: list[dict[str, object]], nm_id: int) -> dict[str, object] | None:
    for product in products:
        if int(product["nm_id"]) == nm_id:
            return product
    return None


async def _send_private_only_notice(update: Update) -> None:
    text = "Для безопасности токенов используйте бота только в личном чате."
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text)
    elif update.message:
        await update.message.reply_text(text)


async def _load_products_for_token(
    token: str,
    context: ContextTypes.DEFAULT_TYPE,
    check_feedback_access: bool,
) -> tuple[list[dict[str, object]], bool, str | None]:
    feedback_base_url: str = context.application.bot_data["feedback_base_url"]
    content_base_url: str = context.application.bot_data["content_base_url"]
    timeout: float = context.application.bot_data["timeout"]
    retries: int = context.application.bot_data["retries"]
    request_pause: float = context.application.bot_data["request_pause"]
    products_api_page_size: int = context.application.bot_data["products_api_page_size"]
    max_products: int = context.application.bot_data["max_products"]

    client = WBClient(
        token=token,
        base_url=feedback_base_url,
        timeout_seconds=timeout,
        retries=retries,
        request_pause=request_pause,
    )

    try:
        try:
            cards, hit_limit = await client.fetch_product_cards(
                content_base_url=content_base_url,
                locale="ru",
                page_size=products_api_page_size,
                max_items=max_products,
            )
        except WBAPIError as exc:
            return [], False, (
                "Не удалось получить список товаров. "
                "У токена должен быть доступ к API товаров (контент).\n\n"
                f"Детали: {exc}"
            )

        if check_feedback_access:
            try:
                check_options = FetchOptions(
                    nm_id=None,
                    answered="false",
                    page_size=1,
                    order="dateDesc",
                    date_from=None,
                    date_to=None,
                    max_items=1,
                )
                await client.fetch_questions(check_options)
            except WBAPIError as exc:
                return [], False, (
                    "Токен не дает доступ к API вопросов/отзывов.\n"
                    "При создании токена включите раздел «Вопросы и отзывы».\n\n"
                    f"Детали: {exc}"
                )

        return _normalize_products(cards), hit_limit, None
    finally:
        await client.close()


async def _ensure_products_cache(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    force_refresh: bool,
    check_feedback_access: bool,
) -> tuple[list[dict[str, object]] | None, bool, str | None]:
    user_id = _user_id(update)
    if user_id is None:
        return None, False, "Не удалось определить пользователя."

    if not force_refresh:
        cached = context.user_data.get(STATE_PRODUCTS)
        cached_at = context.user_data.get(STATE_PRODUCTS_CACHED_AT)
        if isinstance(cached, list):
            try:
                cached_age = time.time() - float(cached_at)
            except (TypeError, ValueError):
                cached_age = CACHE_TTL_SECONDS + 1
            if cached_age <= CACHE_TTL_SECONDS:
                return cached, bool(context.user_data.get(STATE_PRODUCTS_LIMIT_HIT, False)), None

    store: UserTokenStore = context.application.bot_data["token_store"]
    token = await store.get_token(user_id)
    if not token:
        return None, False, "Сначала подключите WB токен."

    products, hit_limit, error_text = await _load_products_for_token(
        token=token,
        context=context,
        check_feedback_access=check_feedback_access,
    )
    if error_text is not None:
        return None, False, error_text

    context.user_data[STATE_PRODUCTS] = products
    context.user_data[STATE_PRODUCTS_LIMIT_HIT] = hit_limit
    context.user_data[STATE_PRODUCTS_CACHED_AT] = time.time()
    if products:
        page_value = context.user_data.get(STATE_PRODUCTS_PAGE, 0)
        try:
            page_int = int(page_value)
        except (TypeError, ValueError):
            page_int = 0
        total_pages = max(1, (len(products) + UI_PRODUCTS_PER_PAGE - 1) // UI_PRODUCTS_PER_PAGE)
        context.user_data[STATE_PRODUCTS_PAGE] = max(0, min(page_int, total_pages - 1))
    else:
        context.user_data[STATE_PRODUCTS_PAGE] = 0

    return products, hit_limit, None


async def _render_products_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int,
    force_refresh: bool,
    check_feedback_access: bool,
) -> None:
    if not _is_private_chat(update):
        await _send_private_only_notice(update)
        return

    user_id = _user_id(update)
    if user_id is None:
        return

    store: UserTokenStore = context.application.bot_data["token_store"]
    has_token = await store.has_token(user_id)

    query = update.callback_query
    message = query.message if query and query.message else update.message
    if message is None:
        return

    if not has_token:
        text = (
            "Привет! Это WB CSV бот.\n\n"
            "1) Подключите свой WB токен\n"
            "2) Получите список товаров\n"
            "3) Выберите товар и скачайте отдельно отзывы или вопросы"
        )
        if query:
            await query.edit_message_text(text, reply_markup=_token_keyboard(has_token=False))
        else:
            await message.reply_text(text, reply_markup=_token_keyboard(has_token=False))
        return

    if query and force_refresh:
        await query.edit_message_text("Загружаю список товаров...")
    elif not query and force_refresh:
        await message.reply_text("Загружаю список товаров...")

    products, hit_limit, error_text = await _ensure_products_cache(
        update=update,
        context=context,
        force_refresh=force_refresh,
        check_feedback_access=check_feedback_access,
    )

    if error_text is not None:
        if query:
            await query.edit_message_text(error_text, reply_markup=_token_keyboard(has_token=True))
        else:
            await message.reply_text(error_text, reply_markup=_token_keyboard(has_token=True))
        return
    if products is None:
        return

    total_pages = max(1, (len(products) + UI_PRODUCTS_PER_PAGE - 1) // UI_PRODUCTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data[STATE_PRODUCTS_PAGE] = page

    text = _products_text(products, page=page, hit_limit=hit_limit)
    markup = _products_keyboard(products, page=page)
    if query:
        await query.edit_message_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(STATE_AWAITING_TOKEN, None)
    await _render_products_message(
        update=update,
        context=context,
        page=0,
        force_refresh=True,
        check_feedback_access=False,
    )


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(STATE_AWAITING_TOKEN, None)
    if update.message:
        await update.message.reply_text("Ок, отменил. Нажмите /start.")


async def _set_token_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private_chat(update):
        await _send_private_only_notice(update)
        return

    context.user_data[STATE_AWAITING_TOKEN] = True
    text = (
        "Отправьте WB API токен.\n\n"
        "При создании токена в WB включите:\n"
        "- «Вопросы и отзывы»\n"
        "- доступ к товарам (контент)\n\n"
        "Пример:\nWB_API_TOKEN=eyJ..."
    )
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text)
    elif update.message:
        await update.message.reply_text(text)


async def _delete_token_and_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _user_id(update)
    if user_id is None:
        return

    store: UserTokenStore = context.application.bot_data["token_store"]
    removed = await store.delete_token(user_id)
    context.user_data.pop(STATE_AWAITING_TOKEN, None)
    context.user_data.pop(STATE_PRODUCTS, None)
    context.user_data.pop(STATE_PRODUCTS_PAGE, None)
    context.user_data.pop(STATE_PRODUCTS_LIMIT_HIT, None)
    context.user_data.pop(STATE_PRODUCTS_CACHED_AT, None)

    text = "Токен удален. Подключите новый токен." if removed else "Сохраненный токен не найден."
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, reply_markup=_token_keyboard(has_token=False))
    elif update.message:
        await update.message.reply_text(text, reply_markup=_token_keyboard(has_token=False))


async def _show_selected_product(update: Update, context: ContextTypes.DEFAULT_TYPE, nm_id: int) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return

    products, _, error_text = await _ensure_products_cache(
        update=update,
        context=context,
        force_refresh=False,
        check_feedback_access=False,
    )
    if error_text is not None:
        await query.edit_message_text(error_text, reply_markup=_token_keyboard(has_token=True))
        return
    if products is None:
        return

    product = _find_product(products, nm_id)
    if product is None:
        await query.edit_message_text(
            "Товар не найден в текущем списке. Обновите список товаров.",
            reply_markup=_token_keyboard(has_token=True),
        )
        return

    text = _product_details_text(product)
    photo_url = product.get("photo_url")
    if isinstance(photo_url, str) and photo_url:
        await query.message.reply_photo(
            photo=photo_url,
            caption=text,
            reply_markup=_product_actions_keyboard(nm_id),
        )
    else:
        await query.message.reply_text(text, reply_markup=_product_actions_keyboard(nm_id))


async def _show_products_list_in_new_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int,
    force_refresh: bool,
    check_feedback_access: bool,
) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return

    products, hit_limit, error_text = await _ensure_products_cache(
        update=update,
        context=context,
        force_refresh=force_refresh,
        check_feedback_access=check_feedback_access,
    )
    if error_text is not None:
        await query.message.reply_text(error_text, reply_markup=_token_keyboard(has_token=True))
        return
    if products is None:
        return

    total_pages = max(1, (len(products) + UI_PRODUCTS_PER_PAGE - 1) // UI_PRODUCTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data[STATE_PRODUCTS_PAGE] = page

    await query.message.reply_text(
        _products_text(products, page=page, hit_limit=hit_limit),
        reply_markup=_products_keyboard(products, page=page),
    )


async def _download_csv_for_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    nm_id: int,
) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    user_id = _user_id(update)
    if user_id is None:
        await query.message.reply_text("Не удалось определить пользователя.")
        return

    store: UserTokenStore = context.application.bot_data["token_store"]
    wb_token = await store.get_token(user_id)
    if not wb_token:
        await query.message.reply_text("Сначала подключите WB токен.", reply_markup=_token_keyboard(False))
        return

    feedback_base_url: str = context.application.bot_data["feedback_base_url"]
    timeout: float = context.application.bot_data["timeout"]
    retries: int = context.application.bot_data["retries"]
    request_pause: float = context.application.bot_data["request_pause"]
    page_size: int = context.application.bot_data["page_size"]

    status = await query.message.reply_text(f"Собираю {ACTION_TITLES[action].lower()} для nmId {nm_id}...")
    client = WBClient(
        token=wb_token,
        base_url=feedback_base_url,
        timeout_seconds=timeout,
        retries=retries,
        request_pause=request_pause,
    )

    tmp_path: Path | None = None
    try:
        if action == ACTION_REVIEWS:
            rows, hit_limit = await client.fetch_feedbacks(
                FetchOptions(
                    nm_id=nm_id,
                    answered="all",
                    page_size=min(max(page_size, 1), MAX_REVIEWS_EXPORT),
                    order="dateDesc",
                    date_from=None,
                    date_to=None,
                    max_items=MAX_REVIEWS_EXPORT,
                )
            )
        else:
            rows, hit_limit = await client.fetch_questions(
                FetchOptions(
                    nm_id=nm_id,
                    answered="all",
                    page_size=page_size,
                    order="dateDesc",
                    date_from=None,
                    date_to=None,
                    max_items=None,
                )
            )

        fd, temp_name = tempfile.mkstemp(prefix=f"{action}_{nm_id}_", suffix=".csv")
        os.close(fd)
        tmp_path = Path(temp_name)
        _write_csv(rows, tmp_path)

        caption = f"{ACTION_TITLES[action]}: {len(rows)} строк."
        if action == ACTION_REVIEWS:
            caption += f" (последние {MAX_REVIEWS_EXPORT})"
        if hit_limit:
            caption += " Достигнут лимит API по пагинации."

        with tmp_path.open("rb") as csv_file:
            await query.message.reply_document(
                document=csv_file,
                filename=f"{action}_{nm_id}.csv",
                caption=caption,
            )
        await status.delete()
    except WBAPIError as exc:
        await status.edit_text(f"Ошибка WB API: {exc}")
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(f"Неожиданная ошибка: {exc}")
    finally:
        await client.close()
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


async def _button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if not _is_private_chat(update):
        await _send_private_only_notice(update)
        return

    callback_data = query.data or ""
    if callback_data == CALLBACK_NOOP:
        return
    if callback_data == CALLBACK_SET_TOKEN:
        await _set_token_prompt(update, context)
        return
    if callback_data == CALLBACK_DELETE_TOKEN:
        await _delete_token_and_reset(update, context)
        return
    if callback_data == CALLBACK_REFRESH_PRODUCTS:
        if query.message and query.message.photo:
            await _show_products_list_in_new_message(
                update=update,
                context=context,
                page=0,
                force_refresh=True,
                check_feedback_access=False,
            )
            return
        await _render_products_message(
            update=update,
            context=context,
            page=0,
            force_refresh=True,
            check_feedback_access=False,
        )
        return
    if callback_data == CALLBACK_BACK_TO_PRODUCTS:
        page_value = context.user_data.get(STATE_PRODUCTS_PAGE, 0)
        try:
            page = int(page_value)
        except (TypeError, ValueError):
            page = 0
        if query.message and query.message.photo:
            await _show_products_list_in_new_message(
                update=update,
                context=context,
                page=page,
                force_refresh=False,
                check_feedback_access=False,
            )
        else:
            await _render_products_message(
                update=update,
                context=context,
                page=page,
                force_refresh=False,
                check_feedback_access=False,
            )
        return

    if callback_data.startswith(CALLBACK_PAGE_PREFIX):
        page_raw = callback_data[len(CALLBACK_PAGE_PREFIX) :]
        try:
            page = int(page_raw)
        except ValueError:
            await query.message.reply_text("Некорректная страница.")
            return
        await _render_products_message(
            update=update,
            context=context,
            page=page,
            force_refresh=False,
            check_feedback_access=False,
        )
        return

    if callback_data.startswith(CALLBACK_SELECT_PREFIX):
        nmid_raw = callback_data[len(CALLBACK_SELECT_PREFIX) :]
        try:
            nm_id = int(nmid_raw)
        except ValueError:
            await query.message.reply_text("Некорректный nmID.")
            return
        await _show_selected_product(update, context, nm_id)
        return

    if callback_data.startswith(CALLBACK_DOWNLOAD_PREFIX):
        payload = callback_data[len(CALLBACK_DOWNLOAD_PREFIX) :]
        try:
            action, nmid_raw = payload.split(":", 1)
            nm_id = int(nmid_raw)
        except (ValueError, TypeError):
            await query.message.reply_text("Некорректные данные для скачивания.")
            return
        if action not in (ACTION_REVIEWS, ACTION_QUESTIONS):
            await query.message.reply_text("Неизвестный тип выгрузки.")
            return
        await _download_csv_for_product(update, context, action, nm_id)
        return

    await query.message.reply_text("Неизвестная кнопка. Нажмите /start.")


async def _handle_token_input(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> None:
    message = update.message
    if message is None:
        return

    token = _extract_token(raw_text)
    if not _looks_like_wb_token(token):
        await message.reply_text("Не похоже на WB токен. Отправьте полный JWT токен.")
        return

    status = await message.reply_text("Проверяю токен и загружаю товары...")
    products, hit_limit, error_text = await _load_products_for_token(
        token=token,
        context=context,
        check_feedback_access=True,
    )
    if error_text is not None:
        await status.edit_text(error_text)
        return

    user_id = _user_id(update)
    if user_id is None:
        await status.edit_text("Не удалось определить пользователя.")
        return

    store: UserTokenStore = context.application.bot_data["token_store"]
    await store.set_token(user_id, token)
    context.user_data.pop(STATE_AWAITING_TOKEN, None)
    context.user_data[STATE_PRODUCTS] = products
    context.user_data[STATE_PRODUCTS_LIMIT_HIT] = hit_limit
    context.user_data[STATE_PRODUCTS_CACHED_AT] = time.time()
    context.user_data[STATE_PRODUCTS_PAGE] = 0

    try:
        await message.delete()
    except TelegramError:
        pass

    await status.edit_text(f"Токен подключен. Найдено товаров: {len(products)}.")
    await message.reply_text(
        _products_text(products, page=0, hit_limit=hit_limit),
        reply_markup=_products_keyboard(products, page=0),
    )


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return

    if not _is_private_chat(update):
        await _send_private_only_notice(update)
        return

    if context.user_data.get(STATE_AWAITING_TOKEN):
        await _handle_token_input(update, context, message.text)
        return

    await message.reply_text("Используйте кнопки в меню. Нажмите /start.")


async def _forget_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private_chat(update):
        await _send_private_only_notice(update)
        return
    await _delete_token_and_reset(update, context)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled bot error: %s", context.error)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram bot для выгрузки отзывов/вопросов WB в CSV.")
    parser.add_argument(
        "--telegram-token",
        default=os.getenv("TELEGRAM_BOT_TOKEN"),
        help="Токен Telegram-бота. Можно задать через TELEGRAM_BOT_TOKEN.",
    )
    parser.add_argument(
        "--token-store",
        default="data/user_tokens.json",
        help="Путь к файлу хранения пользовательских WB токенов (по умолчанию data/user_tokens.json).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Базовый URL API для вопросов/отзывов (по умолчанию {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--content-base-url",
        default=DEFAULT_CONTENT_BASE_URL,
        help=f"Базовый URL API товаров (по умолчанию {DEFAULT_CONTENT_BASE_URL}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Таймаут запроса в секундах (по умолчанию {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Количество повторов при 429/5xx (по умолчанию {DEFAULT_RETRIES}).",
    )
    parser.add_argument(
        "--request-pause",
        type=float,
        default=DEFAULT_REQUEST_PAUSE,
        help=f"Пауза между запросами в секундах (по умолчанию {DEFAULT_REQUEST_PAUSE}).",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Размер страницы для вопросов/отзывов (по умолчанию {DEFAULT_PAGE_SIZE}).",
    )
    parser.add_argument(
        "--products-api-page-size",
        type=int,
        default=100,
        help="Размер страницы WB API товаров (максимум 100).",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=2000,
        help="Максимум товаров для загрузки в список (по умолчанию 2000).",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    if not args.telegram_token:
        print("Ошибка: не найден Telegram токен. Передайте --telegram-token или TELEGRAM_BOT_TOKEN.")
        return 2
    if args.page_size <= 0:
        print("Ошибка: --page-size должен быть > 0.")
        return 2
    if args.products_api_page_size <= 0:
        print("Ошибка: --products-api-page-size должен быть > 0.")
        return 2
    if args.max_products <= 0:
        print("Ошибка: --max-products должен быть > 0.")
        return 2

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )

    app = Application.builder().token(args.telegram_token).build()
    app.bot_data["token_store"] = UserTokenStore(Path(args.token_store))
    app.bot_data["feedback_base_url"] = args.base_url
    app.bot_data["content_base_url"] = args.content_base_url
    app.bot_data["timeout"] = args.timeout
    app.bot_data["retries"] = args.retries
    app.bot_data["request_pause"] = args.request_pause
    app.bot_data["page_size"] = args.page_size
    app.bot_data["products_api_page_size"] = min(args.products_api_page_size, 100)
    app.bot_data["max_products"] = args.max_products

    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("menu", _start))
    app.add_handler(CommandHandler("settoken", _set_token_prompt))
    app.add_handler(CommandHandler("forgettoken", _forget_token))
    app.add_handler(CommandHandler("cancel", _cancel))
    app.add_handler(CallbackQueryHandler(_button_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))
    app.add_error_handler(_on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
