#!/usr/bin/env python3
"""
ПахмаБот — еженедельный понедельничный опрос о состоянии пахмы.
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta

from telegram import Bot, Poll
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import (
    BOT_TOKEN,
    CHANNEL_ID,
    CLOSE_HOUR,
    DATA_DIR,
    GREETING_MESSAGES,
    HISTORY_FILE,
    OPTION_VALUES,
    POLL_HOUR,
    POLL_OPTIONS,
    SEASONAL_MESSAGES,
    WEEKDAY,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pahmabot")

MSK = pytz.timezone("Europe/Moscow")


# ---------------------------------------------------------------------------
# Хранилище данных
# ---------------------------------------------------------------------------

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_history() -> dict:
    """Загружает историю опросов из JSON-файла."""
    ensure_data_dir()
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"polls": [], "current_poll": None}


def save_history(data: dict):
    ensure_data_dir()
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Выбор приветствия
# ---------------------------------------------------------------------------

def pick_greeting() -> str:
    """Выбирает случайное приветствие, с учётом сезона."""
    now = datetime.now(MSK)
    pool = list(GREETING_MESSAGES)
    seasonal = SEASONAL_MESSAGES.get(now.month, [])
    pool.extend(seasonal)
    return random.choice(pool)


# ---------------------------------------------------------------------------
# Подсчёт результатов
# ---------------------------------------------------------------------------

def compute_results(voter_counts: list[int]) -> dict:
    """
    Подсчитывает статистику:
    - average: среднее значение пахмы
    - total_voters: сколько проголосовало
    - phantom_count: сколько выбрали «фантомную пахму»
    - still_drunk: сколько ещё пьют
    """
    total_voters = sum(voter_counts)
    phantom_count = voter_counts[10] if len(voter_counts) > 10 else 0
    still_drunk = voter_counts[0] if voter_counts else 0

    weighted_sum = 0.0
    numeric_voters = 0
    for idx, count in enumerate(voter_counts):
        if idx in OPTION_VALUES and count > 0:
            weighted_sum += OPTION_VALUES[idx] * count
            numeric_voters += count

    average = round(weighted_sum / numeric_voters, 1) if numeric_voters > 0 else 0.0

    return {
        "average": average,
        "total_voters": total_voters,
        "phantom_count": phantom_count,
        "still_drunk": still_drunk,
        "numeric_voters": numeric_voters,
    }


# ---------------------------------------------------------------------------
# Формирование текста итогов
# ---------------------------------------------------------------------------

def format_summary(results: dict, history: dict) -> str:
    """Формирует текст итогового сообщения."""
    avg = results["average"]
    total = results["total_voters"]
    phantom = results["phantom_count"]
    drunk = results["still_drunk"]

    lines = []

    # Основной блок
    lines.append(f"📊 Пахма-итоги дня")
    lines.append(f"")
    lines.append(f"Среднее по чату: {avg}/10")
    lines.append(f"Проголосовали: {_voters_word(total)}.")

    if drunk > 0:
        lines.append(f"Ещё пьют: {drunk} чел. 🍻")

    if phantom > 0:
        word = _phantom_word(phantom)
        lines.append(f"Фантомная пахма: {phantom} {word} 👻")

    # Сравнение с прошлой неделей
    prev = _get_previous_result(history)
    if prev is not None:
        diff = round(avg - prev["average"], 1)
        if abs(diff) >= 1.0:
            if diff > 0:
                lines.append("")
                if avg >= 7:
                    lines.append(f"⚠️ Тяжёлый понедельник! +{diff} к прошлой неделе ({prev['average']}).")
                else:
                    lines.append(f"📈 Пахма подросла: +{diff} к прошлой неделе ({prev['average']}).")
            else:
                lines.append("")
                lines.append(f"📉 Полегчало: {diff} к прошлой неделе ({prev['average']}). Молодцы.")

        # Исторический рекорд
        all_avgs = [p["average"] for p in history.get("polls", []) if "average" in p]
        if all_avgs and avg >= max(all_avgs) and avg >= 5:
            lines.append("🏆 Это рекорд пахмы за всё время наблюдений!")
        elif all_avgs and len(all_avgs) >= 4:
            recent_4 = all_avgs[-4:]
            if avg == max(recent_4) and avg >= 5:
                lines.append("Это самый тяжёлый понедельник за последний месяц.")
            elif avg == min(recent_4) and avg <= 3:
                lines.append("Самый трезвый понедельник за месяц. Уважаю.")

    # Шутка по среднему
    comment = _avg_comment(avg)
    if comment:
        lines.append("")
        lines.append(comment)

    return "\n".join(lines)


def _avg_comment(avg: float) -> str | None:
    if avg <= 1:
        return "Трезвенники. Скучно с вами."
    elif avg <= 2.5:
        return "Лёгкий понедельник. Так держать."
    elif avg <= 4:
        return "Нормальная рабочая пахма."
    elif avg <= 6:
        return "Серьёзная пахма. Сочувствую."
    elif avg <= 8:
        return "Тяжело. Держитесь там."
    elif avg > 8:
        return "Катастрофа. Выживайте."
    return None


def _voters_word(n: int) -> str:
    """Склонение «человек»."""
    if 11 <= n % 100 <= 19:
        return f"{n} человек"
    last = n % 10
    if last == 1:
        return f"{n} человек"
    elif 2 <= last <= 4:
        return f"{n} человека"
    return f"{n} человек"


def _phantom_word(n: int) -> str:
    if 11 <= n % 100 <= 19:
        return "человек"
    last = n % 10
    if last == 1:
        return "человек"
    elif 2 <= last <= 4:
        return "человека"
    return "человек"


def _get_previous_result(history: dict) -> dict | None:
    polls = history.get("polls", [])
    if polls:
        return polls[-1]
    return None


# ---------------------------------------------------------------------------
# Бот: отправка опроса
# ---------------------------------------------------------------------------

async def send_poll(bot: Bot):
    """Отправляет опрос в канал."""
    greeting = pick_greeting()
    logger.info("Отправляю опрос: %s", greeting)

    try:
        message = await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=greeting,
            options=POLL_OPTIONS,
            is_anonymous=False,
            allows_multiple_answers=False,
        )

        history = load_history()
        history["current_poll"] = {
            "message_id": message.message_id,
            "chat_id": message.chat.id,
            "poll_id": message.poll.id,
            "date": datetime.now(MSK).isoformat(),
            "greeting": greeting,
        }
        save_history(history)
        logger.info("Опрос отправлен, message_id=%s", message.message_id)

    except TelegramError as e:
        logger.error("Ошибка отправки опроса: %s", e)


# ---------------------------------------------------------------------------
# Бот: закрытие опроса и итоги
# ---------------------------------------------------------------------------

async def close_poll(bot: Bot):
    """Останавливает опрос, собирает результаты и отправляет итоги."""
    history = load_history()
    current = history.get("current_poll")

    if not current:
        logger.warning("Нет активного опроса для закрытия.")
        return

    try:
        poll: Poll = await bot.stop_poll(
            chat_id=current["chat_id"],
            message_id=current["message_id"],
        )

        voter_counts = [opt.voter_count for opt in poll.options]
        results = compute_results(voter_counts)

        # Сохраняем в историю
        record = {
            "date": current["date"],
            "greeting": current["greeting"],
            "average": results["average"],
            "total_voters": results["total_voters"],
            "phantom_count": results["phantom_count"],
            "still_drunk": results["still_drunk"],
            "voter_counts": voter_counts,
        }

        summary = format_summary(results, history)

        history["polls"].append(record)
        history["current_poll"] = None
        save_history(history)

        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=summary,
        )
        logger.info("Опрос закрыт, среднее: %s", results["average"])

    except TelegramError as e:
        logger.error("Ошибка закрытия опроса: %s", e)


# ---------------------------------------------------------------------------
# Планировщик
# ---------------------------------------------------------------------------

def create_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=MSK)

    # Каждый понедельник в 9:00 MSK
    scheduler.add_job(
        send_poll,
        CronTrigger(day_of_week="mon", hour=POLL_HOUR, minute=0, timezone=MSK),
        args=[bot],
        id="send_poll",
        name="Отправить опрос",
        misfire_grace_time=3600,
    )

    # Каждый понедельник в 18:00 MSK
    scheduler.add_job(
        close_poll,
        CronTrigger(day_of_week="mon", hour=CLOSE_HOUR, minute=0, timezone=MSK),
        args=[bot],
        id="close_poll",
        name="Закрыть опрос",
        misfire_grace_time=3600,
    )

    return scheduler


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан. Создайте .env файл.")
        return
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID не задан. Создайте .env файл.")
        return

    bot = Bot(token=BOT_TOKEN)

    me = await bot.get_me()
    logger.info("Бот запущен: @%s (%s)", me.username, me.first_name)

    scheduler = create_scheduler(bot)
    scheduler.start()

    logger.info(
        "Расписание: опрос каждый понедельник в %02d:00 MSK, "
        "закрытие в %02d:00 MSK",
        POLL_HOUR,
        CLOSE_HOUR,
    )

    # Держим процесс живым
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка бота...")
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
