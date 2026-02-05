# WB parser: отзывы и вопросы

Простой CLI-парсер для выгрузки отзывов и вопросов из Wildberries Seller API в CSV.

## Что умеет
- выгружать `reviews` (отзывы) и `questions` (вопросы);
- фильтровать по `nmId`, датам, наличию ответа;
- собирать данные с пагинацией и ретраями;
- сохранять результат в `CSV` (UTF-8 BOM, удобно для Excel).
- работать как Telegram-бот с активными кнопками для отдельной выгрузки отзывов и вопросов.

## Требования
- Python 3.10+
- API-токен WB категории **«Вопросы и отзывы»**

## Установка
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Быстрый старт
```bash
export WB_API_TOKEN="ваш_токен"
# или так:
# export B_API_TOKEN="ваш_токен"

# Только отзывы по артикулу
python3 main.py reviews --nmid 123456 --output output/reviews_123456.csv

# Только вопросы по артикулу
python3 main.py questions --nmid 123456 --output output/questions_123456.csv

# И отзывы, и вопросы
python3 main.py both --nmid 123456 --out-dir output/123456
```

## Telegram-бот (кнопки + CSV)
Нужны два токена:
- `TELEGRAM_BOT_TOKEN` — токен бота от BotFather;
- `WB_API_TOKEN` или `B_API_TOKEN` — токен WB категории «Вопросы и отзывы».

Запуск:
```bash
source .venv/bin/activate
export TELEGRAM_BOT_TOKEN="ваш_telegram_token"
export WB_API_TOKEN="ваш_wb_token"
python3 telegram_bot.py
```
Шаблон переменных: `.env.example`

В Telegram:
1. Написать `/start`
2. Нажать кнопку **Скачать отзывы CSV** или **Скачать вопросы CSV**
3. Отправить `nmId` (артикул WB)
4. Получить отдельный CSV-файл

## Полезные флаги
- `--answered all|true|false` — фильтр по наличию ответа;
- `--date-from 2025-01-01` и `--date-to 2025-01-31` — фильтр по дате;
- `--order dateDesc|dateAsc` — сортировка по дате;
- `--page-size 1000` — размер страницы;
- `--max-items 5000` — ограничение строк.

Полная справка:
```bash
python3 main.py --help
python3 main.py reviews --help
```

## Важно по лимитам API
Ограничения пагинации задаются самим WB API. Для вопросов лимит жёстче, чем для отзывов. Если лимит достигнут, скрипт покажет предупреждение в stderr.

## Загрузка на GitHub
```bash
git add .
git commit -m "Add WB parser CLI and Telegram bot with CSV download buttons"
git branch -M main
git remote add origin <URL_ВАШЕГО_REPO>
git push -u origin main
```

Важно: не коммитьте реальные токены в репозиторий.
