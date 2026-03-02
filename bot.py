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
    AVG_COMMENTS,
    BOT_TOKEN,
    CHANNEL_ID,
    CLOSE_HOUR,
    DATA_DIR,
    GREETING_MESSAGES,
    HISTORY_FILE,
    HOLIDAY_GREETINGS,
    OPTION_VALUES,
    POLL_HOUR,
    POLL_OPTIONS,
    POLL_TAGLINES,
    RU_HOLIDAYS,
    SEASONAL_MESSAGES,
    SUMMARY_HEADERS,
    ZERO_OPTIONS,
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
# Производственный календарь: рабочие дни
# ---------------------------------------------------------------------------

def is_working_day(d) -> bool:
    """Является ли день рабочим (не выходной и не праздник)."""
    if d.weekday() >= 5:
        return False
    return d.isoformat() not in RU_HOLIDAYS


def get_first_working_day_of_week(d) -> datetime | None:
    """Первый рабочий день ISO-недели, содержащей дату d. None если вся неделя выходная."""
    monday = d - timedelta(days=d.weekday())
    for i in range(5):
        candidate = monday + timedelta(days=i)
        if is_working_day(candidate):
            return candidate
    return None


def get_week_holidays(d) -> list[str]:
    """Праздники, из-за которых первый рабочий день сдвинулся с понедельника.

    Также проверяет предыдущую неделю: если она была полностью нерабочей
    (например, новогодние каникулы), возвращает праздник оттуда.
    """
    monday = d - timedelta(days=d.weekday())
    seen = []
    for i in range(d.weekday()):
        day = monday + timedelta(days=i)
        name = RU_HOLIDAYS.get(day.isoformat())
        if name and name not in seen:
            seen.append(name)

    if not seen and d.weekday() == 0:
        prev_monday = monday - timedelta(days=7)
        if get_first_working_day_of_week(prev_monday) is None:
            for i in range(5):
                day = prev_monday + timedelta(days=i)
                name = RU_HOLIDAYS.get(day.isoformat())
                if name and name not in seen:
                    seen.append(name)

    return seen


# ---------------------------------------------------------------------------
# Выбор приветствия
# ---------------------------------------------------------------------------

def pick_greeting(history: dict) -> str:
    """
    Выбирает приветствие. Приоритет:
    1. Праздничное (если понедельник был выходным из-за праздника)
    2. Сезонное (первый опрос месяца)
    3. Обычное без повторов
    """
    now = datetime.now(MSK)
    today = now.date()

    week_holidays = get_week_holidays(today)
    if week_holidays:
        holiday_name = week_holidays[-1]
        greetings = HOLIDAY_GREETINGS.get(holiday_name, [])
        if greetings:
            history["last_seasonal_month"] = now.month
            return random.choice(greetings)

    seasonal = SEASONAL_MESSAGES.get(now.month, [])
    last_seasonal_month = history.get("last_seasonal_month")
    if seasonal and last_seasonal_month != now.month:
        history["last_seasonal_month"] = now.month
        return random.choice(seasonal)

    used = set(history.get("used_greeting_indices", []))
    available = [
        i for i in range(len(GREETING_MESSAGES)) if i not in used
    ]
    if not available:
        used.clear()
        available = list(range(len(GREETING_MESSAGES)))

    idx = random.choice(available)
    used.add(idx)
    history["used_greeting_indices"] = sorted(used)

    return GREETING_MESSAGES[idx]


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

    lines.append(random.choice(SUMMARY_HEADERS))
    lines.append("")
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
        key = "sober"
    elif avg <= 2.5:
        key = "light"
    elif avg <= 4:
        key = "normal"
    elif avg <= 6:
        key = "serious"
    elif avg <= 8:
        key = "heavy"
    else:
        key = "critical"
    comments = AVG_COMMENTS.get(key, [])
    return random.choice(comments) if comments else None


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

async def maybe_send_poll(bot: Bot):
    """Проверяет, первый ли рабочий день недели, и отправляет опрос."""
    today = datetime.now(MSK).date()

    if not is_working_day(today):
        return

    first_wd = get_first_working_day_of_week(today)
    if first_wd != today:
        logger.debug(
            "Сегодня %s — не первый рабочий день недели (первый: %s), пропускаем.",
            today, first_wd,
        )
        return

    logger.info("Сегодня %s — первый рабочий день недели, запускаем опрос.", today)
    await send_poll(bot)


def _build_poll_options() -> list[str]:
    """Собирает варианты ответа, подставляя случайный текст для 0/10."""
    options = list(POLL_OPTIONS)
    options[1] = random.choice(ZERO_OPTIONS)
    return options


async def send_poll(bot: Bot):
    """Отправляет опрос в канал."""
    history = load_history()
    greeting = pick_greeting(history)
    tagline = random.choice(POLL_TAGLINES)
    question = f"{greeting}\n{tagline}"
    options = _build_poll_options()
    logger.info("Отправляю опрос: %s", question)

    try:
        message = await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=question,
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
        )

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

async def maybe_close_poll(bot: Bot):
    """Закрывает активный опрос, если он есть."""
    history = load_history()
    if not history.get("current_poll"):
        return
    await close_poll(bot)


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

    # Каждый будний день в POLL_HOUR — проверка, первый ли рабочий день недели
    scheduler.add_job(
        maybe_send_poll,
        CronTrigger(day_of_week="mon-fri", hour=POLL_HOUR, minute=0, timezone=MSK),
        args=[bot],
        id="maybe_send_poll",
        name="Проверить и отправить опрос",
        misfire_grace_time=3600,
    )

    # Каждый будний день в CLOSE_HOUR — закрытие, если есть активный опрос
    scheduler.add_job(
        maybe_close_poll,
        CronTrigger(day_of_week="mon-fri", hour=CLOSE_HOUR, minute=0, timezone=MSK),
        args=[bot],
        id="maybe_close_poll",
        name="Проверить и закрыть опрос",
        misfire_grace_time=3600,
    )

    return scheduler


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def recover_after_restart(bot: Bot):
    """Обрабатывает пропущенные действия после перезапуска бота."""
    now = datetime.now(MSK)
    today = now.date()
    hour = now.hour
    history = load_history()
    current = history.get("current_poll")

    if current:
        if hour >= CLOSE_HOUR:
            logger.info("Найден незакрытый опрос после перезапуска, закрываю.")
            await close_poll(bot)
        else:
            logger.info(
                "Найден активный опрос (message_id=%s), закрытие в %02d:00.",
                current["message_id"], CLOSE_HOUR,
            )
    elif hour >= POLL_HOUR and hour < CLOSE_HOUR:
        first_wd = get_first_working_day_of_week(today)
        if first_wd == today and is_working_day(today):
            logger.info("Пропущен опрос после перезапуска, отправляю.")
            await send_poll(bot)


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

    await recover_after_restart(bot)

    scheduler = create_scheduler(bot)
    scheduler.start()

    logger.info(
        "Расписание: опрос в первый рабочий день недели в %02d:00 MSK, "
        "закрытие в %02d:00 MSK (производственный календарь РФ)",
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
