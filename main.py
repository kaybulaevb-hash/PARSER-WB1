#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx


DEFAULT_BASE_URL = "https://feedbacks-api.wildberries.ru"
DEFAULT_CONTENT_BASE_URL = "https://content-api.wildberries.ru"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRIES = 5
DEFAULT_PAGE_SIZE = 1000
DEFAULT_REQUEST_PAUSE = 0.35
ORDER_VALUES = ("dateAsc", "dateDesc")
ANSWERED_VALUES = ("all", "true", "false")


class WBAPIError(RuntimeError):
    pass


def _token_from_env() -> str | None:
    return os.getenv("WB_API_TOKEN") or os.getenv("B_API_TOKEN")


@dataclass
class FetchOptions:
    nm_id: int | None
    answered: str
    page_size: int
    order: str
    date_from: int | None
    date_to: int | None
    max_items: int | None


class WBClient:
    def __init__(
        self,
        token: str,
        base_url: str,
        timeout_seconds: float,
        retries: int,
        request_pause: float,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._retries = retries
        self._request_pause = request_pause
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        override_base_url: str | None = None,
    ) -> dict[str, Any]:
        base_url = (override_base_url or self._base_url).rstrip("/")
        url = f"{base_url}{path}"
        headers = {
            "Authorization": self._token,
            "Accept": "application/json",
        }

        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
            except httpx.RequestError as exc:
                if attempt == self._retries:
                    raise WBAPIError(f"Сетевая ошибка: {exc}") from exc
                await asyncio.sleep(min(2**attempt, 10))
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == self._retries:
                    raise WBAPIError(
                        f"WB API вернул {response.status_code}: {response.text[:200]}"
                    )
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = max(float(retry_after), 0.5)
                else:
                    delay = min(2**attempt, 10)
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                raise WBAPIError(
                    f"WB API вернул {response.status_code}: {response.text[:300]}"
                )

            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise WBAPIError("WB API вернул не-JSON ответ.") from exc

            if isinstance(payload, dict) and payload.get("error") is True:
                error_text = payload.get("errorText") or "Неизвестная ошибка WB API"
                raise WBAPIError(str(error_text))

            if not isinstance(payload, dict):
                raise WBAPIError("Неожиданный формат ответа WB API.")
            return payload

        raise WBAPIError("Не удалось выполнить запрос к WB API.")

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json("GET", path=path, params=params)

    async def _post_json(
        self,
        path: str,
        json_body: dict[str, Any],
        params: dict[str, Any] | None = None,
        override_base_url: str | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            path=path,
            params=params,
            json_body=json_body,
            override_base_url=override_base_url,
        )

    async def fetch_feedbacks(self, options: FetchOptions) -> tuple[list[dict[str, Any]], bool]:
        return await self._fetch_items(
            path="/api/v1/feedbacks",
            item_key="feedbacks",
            options=options,
            max_take=5000,
            max_skip=199_990,
            max_take_plus_skip=None,
        )

    async def fetch_questions(self, options: FetchOptions) -> tuple[list[dict[str, Any]], bool]:
        return await self._fetch_items(
            path="/api/v1/questions",
            item_key="questions",
            options=options,
            max_take=10_000,
            max_skip=10_000,
            max_take_plus_skip=10_000,
        )

    async def fetch_product_cards(
        self,
        content_base_url: str = DEFAULT_CONTENT_BASE_URL,
        locale: str = "ru",
        page_size: int = 100,
        max_items: int | None = 2000,
    ) -> tuple[list[dict[str, Any]], bool]:
        if page_size <= 0:
            raise ValueError("page_size должен быть > 0")
        page_size = min(page_size, 100)

        cursor_updated_at: str | None = None
        cursor_nm_id: int | None = None
        cards: list[dict[str, Any]] = []
        hit_limit = False

        while True:
            cursor: dict[str, Any] = {"limit": page_size}
            if cursor_updated_at is not None and cursor_nm_id is not None:
                cursor["updatedAt"] = cursor_updated_at
                cursor["nmID"] = cursor_nm_id

            body = {
                "settings": {
                    "sort": {"ascending": False},
                    "filter": {"withPhoto": -1},
                    "cursor": cursor,
                }
            }
            params = {"locale": locale} if locale else None
            payload = await self._post_json(
                path="/content/v2/get/cards/list",
                json_body=body,
                params=params,
                override_base_url=content_base_url,
            )

            raw_cards = payload.get("cards")
            if not isinstance(raw_cards, list):
                raise WBAPIError("Неожиданный формат ответа WB API (cards).")
            current_cards = [card for card in raw_cards if isinstance(card, dict)]
            cards.extend(current_cards)

            if max_items is not None and len(cards) >= max_items:
                cards = cards[:max_items]
                hit_limit = True
                break

            if len(current_cards) < page_size:
                break

            cursor_payload = payload.get("cursor")
            if not isinstance(cursor_payload, dict):
                break
            next_updated_at = cursor_payload.get("updatedAt")
            next_nm_id_raw = cursor_payload.get("nmID")
            try:
                next_nm_id = int(next_nm_id_raw)
            except (TypeError, ValueError):
                break
            if not next_updated_at or next_nm_id <= 0:
                break

            cursor_updated_at = str(next_updated_at)
            cursor_nm_id = next_nm_id
            await asyncio.sleep(self._request_pause)

        return cards, hit_limit

    async def _fetch_items(
        self,
        path: str,
        item_key: str,
        options: FetchOptions,
        max_take: int,
        max_skip: int,
        max_take_plus_skip: int | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        answered_values = _answered_mode_to_values(options.answered)
        results: list[dict[str, Any]] = []
        hit_limit = False
        reached_max_items = False

        for is_answered in answered_values:
            skip = 0
            while True:
                take = min(options.page_size, max_take)
                if max_take_plus_skip is not None:
                    available = max_take_plus_skip - skip
                    if available <= 0:
                        hit_limit = True
                        break
                    take = min(take, available)

                params: dict[str, Any] = {
                    "isAnswered": str(is_answered).lower(),
                    "take": take,
                    "skip": skip,
                    "order": options.order,
                }
                if options.nm_id is not None:
                    params["nmId"] = options.nm_id
                if options.date_from is not None:
                    params["dateFrom"] = options.date_from
                if options.date_to is not None:
                    params["dateTo"] = options.date_to

                payload = await self._get_json(path, params=params)
                data = payload.get("data")
                items = _extract_items(data, item_key)
                if not items:
                    break

                for item in items:
                    if isinstance(item, dict):
                        item["_query_is_answered"] = is_answered
                results.extend(items)

                if options.max_items is not None and len(results) >= options.max_items:
                    deduped_now = _dedupe_by_id(results)
                    if len(deduped_now) >= options.max_items:
                        reached_max_items = True
                        break

                fetched_count = len(items)
                skip += fetched_count
                if fetched_count < take:
                    break
                if skip > max_skip:
                    hit_limit = True
                    break

                await asyncio.sleep(self._request_pause)
            if reached_max_items:
                break

        deduped = _dedupe_by_id(results)
        if options.max_items is not None and len(deduped) > options.max_items:
            deduped = deduped[: options.max_items]
        return deduped, hit_limit


def _answered_mode_to_values(answered: str) -> list[bool]:
    if answered == "true":
        return [True]
    if answered == "false":
        return [False]
    return [False, True]


def _extract_items(data: Any, item_key: str) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        items = data.get(item_key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _dedupe_by_id(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    unique: list[dict[str, Any]] = []
    without_id: list[dict[str, Any]] = []

    for item in items:
        item_id = item.get("id")
        if item_id is None:
            without_id.append(item)
            continue
        key = str(item_id)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        unique.append(item)

    unique.extend(without_id)
    return unique


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            new_prefix = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                flat.update(_flatten(value, new_prefix))
            elif isinstance(value, list):
                flat[new_prefix] = json.dumps(value, ensure_ascii=False)
            else:
                flat[new_prefix] = value
    else:
        flat[prefix or "value"] = obj
    return flat


def _write_csv(rows: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    flattened = [_flatten(row) for row in rows]
    field_names = sorted({key for row in flattened for key in row.keys()})

    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=field_names)
        writer.writeheader()
        writer.writerows(flattened)


def _parse_date_to_unix(value: str, end_of_day: bool) -> int:
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        dt_date = datetime.strptime(value, "%Y-%m-%d")
        dt = datetime.combine(
            dt_date.date(), time.max if end_of_day else time.min, tzinfo=timezone.utc
        )
    else:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if "T" not in value:
            dt = datetime.combine(
                dt.date(), time.max if end_of_day else time.min, tzinfo=timezone.utc
            )

    return int(dt.timestamp())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Парсер отзывов и вопросов Wildberries (WB Seller API).",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--token",
        default=_token_from_env(),
        help="WB API токен категории 'Вопросы и отзывы'. Можно не указывать, если задан WB_API_TOKEN или B_API_TOKEN.",
    )
    common.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Базовый URL API (по умолчанию {DEFAULT_BASE_URL}).",
    )
    common.add_argument("--nmid", type=int, help="Артикул WB (nmId). Если не указан — по всем товарам.")
    common.add_argument(
        "--answered",
        choices=ANSWERED_VALUES,
        default="all",
        help="Фильтр по наличию ответа: all | true | false. По умолчанию all.",
    )
    common.add_argument(
        "--order",
        choices=ORDER_VALUES,
        default="dateDesc",
        help="Сортировка по дате: dateAsc/dateDesc.",
    )
    common.add_argument(
        "--date-from",
        help="Начальная дата (ISO: YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS).",
    )
    common.add_argument(
        "--date-to",
        help="Конечная дата (ISO: YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS).",
    )
    common.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Размер страницы (по умолчанию {DEFAULT_PAGE_SIZE}).",
    )
    common.add_argument("--max-items", type=int, help="Ограничить общее число строк в выгрузке.")
    common.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Таймаут запроса в секундах (по умолчанию {DEFAULT_TIMEOUT_SECONDS}).",
    )
    common.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Количество повторов при ошибках сети/429/5xx (по умолчанию {DEFAULT_RETRIES}).",
    )
    common.add_argument(
        "--request-pause",
        type=float,
        default=DEFAULT_REQUEST_PAUSE,
        help=f"Пауза между запросами в секундах (по умолчанию {DEFAULT_REQUEST_PAUSE}).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    reviews = subparsers.add_parser("reviews", parents=[common], help="Собрать отзывы.")
    reviews.add_argument("--output", default="output/reviews.csv", help="Путь к CSV-файлу.")

    questions = subparsers.add_parser("questions", parents=[common], help="Собрать вопросы.")
    questions.add_argument("--output", default="output/questions.csv", help="Путь к CSV-файлу.")

    both = subparsers.add_parser("both", parents=[common], help="Собрать отзывы и вопросы.")
    both.add_argument("--out-dir", default="output", help="Папка для двух CSV-файлов.")

    return parser


async def _run(args: argparse.Namespace) -> int:
    if not args.token:
        print(
            "Ошибка: не найден WB API токен. Передайте --token или задайте WB_API_TOKEN/B_API_TOKEN.",
            file=sys.stderr,
        )
        return 2
    if args.page_size <= 0:
        print("Ошибка: --page-size должен быть > 0.", file=sys.stderr)
        return 2

    try:
        date_from = _parse_date_to_unix(args.date_from, end_of_day=False) if args.date_from else None
        date_to = _parse_date_to_unix(args.date_to, end_of_day=True) if args.date_to else None
    except ValueError:
        print(
            "Ошибка: неверный формат даты. Используйте YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS.",
            file=sys.stderr,
        )
        return 2
    if date_from is not None and date_to is not None and date_from > date_to:
        print("Ошибка: --date-from не может быть позже --date-to.", file=sys.stderr)
        return 2

    options = FetchOptions(
        nm_id=args.nmid,
        answered=args.answered,
        page_size=args.page_size,
        order=args.order,
        date_from=date_from,
        date_to=date_to,
        max_items=args.max_items,
    )

    client = WBClient(
        token=args.token,
        base_url=args.base_url,
        timeout_seconds=args.timeout,
        retries=args.retries,
        request_pause=args.request_pause,
    )

    try:
        if args.command == "reviews":
            rows, hit_limit = await client.fetch_feedbacks(options)
            output_path = Path(args.output)
            _write_csv(rows, output_path)
            print(f"Отзывы: {len(rows)} строк -> {output_path}")
            if hit_limit:
                print("Внимание: достигнут лимит API по пагинации для одного запроса.", file=sys.stderr)

        elif args.command == "questions":
            rows, hit_limit = await client.fetch_questions(options)
            output_path = Path(args.output)
            _write_csv(rows, output_path)
            print(f"Вопросы: {len(rows)} строк -> {output_path}")
            if hit_limit:
                print("Внимание: достигнут лимит API по пагинации для одного запроса.", file=sys.stderr)

        elif args.command == "both":
            out_dir = Path(args.out_dir)
            reviews_rows, reviews_hit_limit = await client.fetch_feedbacks(options)
            questions_rows, questions_hit_limit = await client.fetch_questions(options)

            reviews_path = out_dir / "reviews.csv"
            questions_path = out_dir / "questions.csv"
            _write_csv(reviews_rows, reviews_path)
            _write_csv(questions_rows, questions_path)
            print(f"Отзывы: {len(reviews_rows)} строк -> {reviews_path}")
            print(f"Вопросы: {len(questions_rows)} строк -> {questions_path}")
            if reviews_hit_limit or questions_hit_limit:
                print("Внимание: достигнут лимит API по пагинации для одного из запросов.", file=sys.stderr)
        else:
            print("Неизвестная команда.", file=sys.stderr)
            return 2
    except WBAPIError as exc:
        print(f"Ошибка WB API: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()

    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
