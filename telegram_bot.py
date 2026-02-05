#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    DEFAULT_PAGE_SIZE,
    DEFAULT_REQUEST_PAUSE,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    FetchOptions,
    WBAPIError,
    WBClient,
    _token_from_env,
    _write_csv,
)


ACTION_REVIEWS = "reviews"
ACTION_QUESTIONS = "questions"
CALLBACK_TO_ACTION = {
    "download_reviews": ACTION_REVIEWS,
    "download_questions": ACTION_QUESTIONS,
}
ACTION_TITLES = {
    ACTION_REVIEWS: "Отзывы",
    ACTION_QUESTIONS: "Вопросы",
}


def _download_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Скачать отзывы CSV", callback_data="download_reviews")],
            [InlineKeyboardButton("Скачать вопросы CSV", callback_data="download_questions")],
        ]
    )


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_action", None)
    text = (
        "Привет! Я выгружаю CSV из WB Seller API.\n\n"
        "1) Нажми кнопку\n"
        "2) Пришли артикул WB (nmId)\n"
        "3) Получи CSV файлом"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=_download_keyboard())
    elif update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=_download_keyboard())


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_action", None)
    if update.message:
        await update.message.reply_text("Ок, отменил. Нажми /start для нового запроса.")


async def _button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    action = CALLBACK_TO_ACTION.get(query.data or "")
    if action is None:
        await query.edit_message_text("Неизвестная кнопка. Нажми /start.")
        return

    context.user_data["pending_action"] = action
    await query.edit_message_text(
        f"Выбрано: {ACTION_TITLES[action]}.\nПришли артикул WB (nmId), например: 436508518"
    )


async def _handle_nmid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or not message.text:
        return

    action = context.user_data.get("pending_action")
    if action not in (ACTION_REVIEWS, ACTION_QUESTIONS):
        await message.reply_text("Нажми /start и выбери, что выгрузить.")
        return

    raw_nmid = message.text.strip()
    if not raw_nmid.isdigit():
        await message.reply_text("Неверный nmId. Пришли число, например 436508518.")
        return

    nmid = int(raw_nmid)
    status = await message.reply_text(f"Собираю {ACTION_TITLES[action].lower()} для nmId {nmid}...")

    wb_token: str = context.application.bot_data["wb_token"]
    base_url: str = context.application.bot_data["base_url"]
    timeout: float = context.application.bot_data["timeout"]
    retries: int = context.application.bot_data["retries"]
    request_pause: float = context.application.bot_data["request_pause"]
    page_size: int = context.application.bot_data["page_size"]

    client = WBClient(
        token=wb_token,
        base_url=base_url,
        timeout_seconds=timeout,
        retries=retries,
        request_pause=request_pause,
    )

    tmp_path: Path | None = None
    try:
        fetch_options = FetchOptions(
            nm_id=nmid,
            answered="all",
            page_size=page_size,
            order="dateDesc",
            date_from=None,
            date_to=None,
            max_items=None,
        )

        if action == ACTION_REVIEWS:
            rows, hit_limit = await client.fetch_feedbacks(fetch_options)
        else:
            rows, hit_limit = await client.fetch_questions(fetch_options)

        fd, name = tempfile.mkstemp(prefix=f"{action}_{nmid}_", suffix=".csv")
        os.close(fd)
        tmp_path = Path(name)
        _write_csv(rows, tmp_path)

        caption = f"{ACTION_TITLES[action]}: {len(rows)} строк."
        if hit_limit:
            caption += " Достигнут лимит API по пагинации."

        with tmp_path.open("rb") as f:
            await message.reply_document(
                document=f,
                filename=f"{action}_{nmid}.csv",
                caption=caption,
            )

        await status.delete()
        await message.reply_text("Готово. Можно выгрузить снова:", reply_markup=_download_keyboard())
    except WBAPIError as exc:
        await status.edit_text(f"Ошибка WB API: {exc}")
    except Exception as exc:  # noqa: BLE001
        await status.edit_text(f"Неожиданная ошибка: {exc}")
    finally:
        context.user_data.pop("pending_action", None)
        await client.close()
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


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
        "--wb-token",
        default=_token_from_env(),
        help="WB API токен. Можно задать через WB_API_TOKEN или B_API_TOKEN.",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Базовый URL API (по умолчанию {DEFAULT_BASE_URL}).",
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
        help=f"Размер страницы (по умолчанию {DEFAULT_PAGE_SIZE}).",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    if not args.telegram_token:
        print("Ошибка: не найден Telegram токен. Передайте --telegram-token или TELEGRAM_BOT_TOKEN.")
        return 2
    if not args.wb_token:
        print("Ошибка: не найден WB токен. Передайте --wb-token или WB_API_TOKEN/B_API_TOKEN.")
        return 2
    if args.page_size <= 0:
        print("Ошибка: --page-size должен быть > 0.")
        return 2

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )

    app = Application.builder().token(args.telegram_token).build()
    app.bot_data["wb_token"] = args.wb_token
    app.bot_data["base_url"] = args.base_url
    app.bot_data["timeout"] = args.timeout
    app.bot_data["retries"] = args.retries
    app.bot_data["request_pause"] = args.request_pause
    app.bot_data["page_size"] = args.page_size

    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("menu", _start))
    app.add_handler(CommandHandler("cancel", _cancel))
    app.add_handler(CallbackQueryHandler(_button_click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_nmid))
    app.add_error_handler(_on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
