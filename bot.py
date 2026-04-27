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
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
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
    IDX_PHANTOM,
    IDX_SOBER,
    IDX_STILL_DRUNK,
    OPTION_VALUES,
    POLL_HOUR,
    POLL_OPTIONS,
    POLL_TAGLINES,
    REVEAL_PHRASES,
    RU_HOLIDAYS,
    SEND_POLL_MAX_ATTEMPTS,
    SEND_POLL_RETRY_DELAY_SECONDS,
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
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Повреждён history.json, создаю резервную копию: %s", e)
            backup = HISTORY_FILE + ".bak"
            if os.path.exists(HISTORY_FILE):
                os.replace(HISTORY_FILE, backup)
    return {"polls": [], "current_poll": None}


def save_history(data: dict):
    ensure_data_dir()
    tmp_path = HISTORY_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, HISTORY_FILE)


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
    total_voters = sum(voter_counts)
    sober_count = voter_counts[IDX_SOBER] if len(voter_counts) > IDX_SOBER else 0
    still_drunk = voter_counts[IDX_STILL_DRUNK] if len(voter_counts) > IDX_STILL_DRUNK else 0
    phantom_count = voter_counts[IDX_PHANTOM] if len(voter_counts) > IDX_PHANTOM else 0

    weighted_sum = 0.0
    numeric_voters = 0
    hangover_sum = 0.0
    hangover_count = 0
    for idx, count in enumerate(voter_counts):
        if idx in OPTION_VALUES and count > 0:
            weighted_sum += OPTION_VALUES[idx] * count
            numeric_voters += count
            if OPTION_VALUES[idx] > 0 and idx != IDX_STILL_DRUNK:
                hangover_sum += OPTION_VALUES[idx] * count
                hangover_count += count

    average = round(weighted_sum / numeric_voters, 1) if numeric_voters > 0 else 0.0
    hangover_avg = round(hangover_sum / hangover_count, 1) if hangover_count > 0 else 0.0

    return {
        "average": average,
        "hangover_avg": hangover_avg,
        "hangover_count": hangover_count,
        "sober_count": sober_count,
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
    sober = results["sober_count"]
    hangover_count = results["hangover_count"]
    hangover_avg = results["hangover_avg"]
    custom_options = results.get("custom_options", [])
    polls = history.get("polls", [])
    poll_number = len(polls) + 1

    lines = []

    lines.append(random.choice(REVEAL_PHRASES))
    lines.append("")
    lines.append(f"{random.choice(SUMMARY_HEADERS)} (#{poll_number})")
    lines.append("")
    lines.append(f"Проголосовали: {_voters_word(total)}.")

    if total > 0:
        sober_pct = round(sober / total * 100)
        with_pahma = hangover_count + drunk
        if with_pahma > 0:
            lines.append(f"Трезвых: {sober} ({sober_pct}%), с пахмой: {with_pahma}.")
        else:
            lines.append(f"Трезвых: {sober} ({sober_pct}%).")

    lines.append("")
    lines.append(f"Средняя пахма по чату: {avg}/10")
    if hangover_count > 0:
        lines.append(f"Средняя среди похмельных: {hangover_avg}/10")

    if drunk > 0:
        lines.append(f"Ещё пьют: {drunk} чел. 🍻")

    if phantom > 0:
        word = _people_word(phantom)
        lines.append(f"Фантомная пахма: {phantom} {word} 👻")

    # Сравнение с прошлой неделей
    prev = _get_previous_result(history)
    if prev is not None:
        prev_avg = prev["average"]
        diff = round(avg - prev_avg, 1)
        if abs(diff) >= 0.5:
            lines.append("")
            if diff > 0:
                if avg >= 7:
                    lines.append(f"⚠️ Тяжёлая неделя! +{diff} к прошлой ({prev_avg}).")
                else:
                    lines.append(f"📈 Пахма подросла: +{diff} к прошлой неделе ({prev_avg}).")
            else:
                lines.append(f"📉 Полегчало: {diff} к прошлой неделе ({prev_avg}).")

        prev_hangover = prev.get("hangover_count", prev.get("drinker_count", 0))
        if hangover_count > 0 and prev_hangover > 0:
            diff_h = hangover_count - prev_hangover
            if abs(diff_h) >= 2:
                if diff_h > 0:
                    lines.append(f"Похмельных стало больше: {hangover_count} vs {prev_hangover}.")
                else:
                    lines.append(f"Похмельных стало меньше: {hangover_count} vs {prev_hangover}.")

    # Рекорды и антирекорды
    all_avgs = [p["average"] for p in polls if "average" in p]
    if all_avgs:
        if avg > 0 and avg >= max(all_avgs):
            lines.append("🏆 Рекорд пахмы за всё время!")
        elif avg <= min(all_avgs) and len(all_avgs) >= 3:
            lines.append("🧊 Антирекорд! Самая трезвая неделя за всю историю.")

        if len(all_avgs) >= 4:
            recent_4 = all_avgs[-4:]
            if avg > 0 and avg == max(recent_4):
                lines.append("Самая тяжёлая неделя за последний месяц.")
            elif avg == min(recent_4) and avg < max(recent_4):
                lines.append("Самая лёгкая неделя за месяц.")

    # Серия трезвости
    sober_streak = _count_sober_streak(polls, avg)
    if sober_streak >= 2:
        lines.append(f"🧘 Серия трезвости: {sober_streak} недель подряд средняя < 1!")

    # Пользовательские варианты
    voted_custom = [c for c in custom_options if c["votes"] > 0]
    if voted_custom:
        lines.append("")
        lines.append("✏️ Народное творчество:")
        for c in voted_custom:
            lines.append(f'  • «{c["text"]}» — {c["votes"]} гол.')
    elif custom_options:
        lines.append("")
        lines.append("✏️ Народное творчество было, но никто не проголосовал.")

    # Комментарий — по средней среди похмельных (если есть), иначе по общей
    comment_avg = hangover_avg if hangover_count > 0 else avg
    comment = _avg_comment(comment_avg)
    if comment:
        lines.append("")
        lines.append(comment)

    # Историческая статистика (каждые 10 опросов)
    if poll_number >= 5 and poll_number % 10 == 0:
        avgs_with_current = all_avgs + [avg]
        all_time_avg = round(sum(avgs_with_current) / len(avgs_with_current), 1)
        lines.append("")
        lines.append(f"📈 За {poll_number} опросов: средняя {all_time_avg}/10, "
                     f"макс. {max(avgs_with_current)}, мин. {min(avgs_with_current)}.")

    return "\n".join(lines)


def _count_sober_streak(polls: list[dict], current_avg: float) -> int:
    """Считает текущую серию недель со средней < 1 (включая текущую)."""
    if current_avg >= 1:
        return 0
    streak = 1
    for p in reversed(polls):
        if p.get("average", 10) < 1:
            streak += 1
        else:
            break
    return streak


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
    return f"{n} {_people_word(n)}"


def _people_word(n: int) -> str:
    """Склонение слова «человек»."""
    if 11 <= n % 100 <= 19:
        return "человек"
    last = n % 10
    if 2 <= last <= 4:
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

    history = load_history()
    if history.get("current_poll"):
        logger.debug("Опрос уже отправлен сегодня, пропускаем.")
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
    options[IDX_SOBER] = random.choice(ZERO_OPTIONS)
    return options


def _is_retryable_api_error(err: TelegramError) -> bool:
    """Временные сбои API/сети — имеет смысл повторить запрос."""
    if isinstance(err, (TimedOut, NetworkError)):
        return True
    text = str(err).lower()
    retry_markers = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "bad gateway",
        "gateway timeout",
        "internal server error",
    )
    return any(marker in text for marker in retry_markers)


async def send_poll(bot: Bot):
    """Отправляет опрос в канал."""
    history = load_history()
    greeting = pick_greeting(history)
    options = _build_poll_options()

    tagline = random.choice(POLL_TAGLINES)
    description = (
        f"{tagline}\n\n"
        "🔒 Результаты скрыты до закрытия опроса\n"
        "✏️ Можешь добавить свой вариант ответа"
    )

    now = datetime.now(MSK)
    close_time = now.replace(hour=23, minute=59, second=0, microsecond=0)
    close_timestamp = int(close_time.timestamp())

    logger.info("Отправляю опрос: %s", greeting)
    for attempt in range(1, SEND_POLL_MAX_ATTEMPTS + 1):
        try:
            message = await bot.send_poll(
                chat_id=CHANNEL_ID,
                question=greeting,
                options=options,
                is_anonymous=False,
                allows_multiple_answers=False,
                close_date=close_timestamp,
                api_kwargs={
                    "description": description,
                    "hide_results_until_closes": True,
                    "allow_adding_options": True,
                },
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
            return
        except RetryAfter as e:
            if attempt >= SEND_POLL_MAX_ATTEMPTS:
                logger.error("Ошибка отправки опроса после %s попыток: %s", attempt, e)
                return
            delay = max(int(getattr(e, "retry_after", 0)), SEND_POLL_RETRY_DELAY_SECONDS)
            logger.warning(
                "Ограничение Telegram API. Повтор отправки через %s сек (попытка %s/%s).",
                delay,
                attempt + 1,
                SEND_POLL_MAX_ATTEMPTS,
            )
            await asyncio.sleep(delay)
        except TelegramError as e:
            if attempt >= SEND_POLL_MAX_ATTEMPTS or not _is_retryable_api_error(e):
                logger.error("Ошибка отправки опроса: %s", e)
                return
            delay = SEND_POLL_RETRY_DELAY_SECONDS * attempt
            logger.warning(
                "Временная ошибка отправки (%s). Повтор через %s сек (попытка %s/%s).",
                e,
                delay,
                attempt + 1,
                SEND_POLL_MAX_ATTEMPTS,
            )
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Бот: закрытие опроса и итоги
# ---------------------------------------------------------------------------

async def maybe_close_poll(bot: Bot):
    """Закрывает активный опрос, если он есть."""
    history = load_history()
    if not history.get("current_poll"):
        return
    await close_poll(bot)


async def _get_poll_from_message(bot: Bot, current: dict) -> Poll | None:
    """Пытается получить Poll из уже закрытого опроса через пересылку сообщения."""
    for attempt in range(1, SEND_POLL_MAX_ATTEMPTS + 1):
        try:
            fwd = await bot.forward_message(
                chat_id=current["chat_id"],
                from_chat_id=current["chat_id"],
                message_id=current["message_id"],
            )
            poll = fwd.poll
            try:
                await bot.delete_message(chat_id=current["chat_id"], message_id=fwd.message_id)
            except TelegramError:
                pass
            return poll
        except RetryAfter as e:
            if attempt >= SEND_POLL_MAX_ATTEMPTS:
                logger.error("forward_message: превышен лимит после %s попыток: %s", attempt, e)
                return None
            delay = max(int(getattr(e, "retry_after", 0)), SEND_POLL_RETRY_DELAY_SECONDS)
            logger.warning(
                "forward_message: RetryAfter, ждём %s сек (%s/%s).",
                delay,
                attempt + 1,
                SEND_POLL_MAX_ATTEMPTS,
            )
            await asyncio.sleep(delay)
        except TelegramError as e:
            if attempt >= SEND_POLL_MAX_ATTEMPTS or not _is_retryable_api_error(e):
                logger.error("Не удалось переслать сообщение опроса: %s", e)
                return None
            delay = SEND_POLL_RETRY_DELAY_SECONDS * attempt
            logger.warning(
                "forward_message: временная ошибка, повтор через %s сек: %s",
                delay,
                e,
            )
            await asyncio.sleep(delay)
    return None


async def _stop_poll_resilient(bot: Bot, current: dict) -> Poll | None:
    """stop_poll с ретраями; при неудаче — poll через forward (уже закрыт или обход таймаута)."""
    chat_id = current["chat_id"]
    message_id = current["message_id"]

    for attempt in range(1, SEND_POLL_MAX_ATTEMPTS + 1):
        try:
            return await bot.stop_poll(
                chat_id=chat_id,
                message_id=message_id,
            )
        except RetryAfter as e:
            if attempt >= SEND_POLL_MAX_ATTEMPTS:
                break
            delay = max(int(getattr(e, "retry_after", 0)), SEND_POLL_RETRY_DELAY_SECONDS)
            logger.warning(
                "stop_poll: лимит API, ждём %s сек (попытка %s/%s).",
                delay,
                attempt + 1,
                SEND_POLL_MAX_ATTEMPTS,
            )
            await asyncio.sleep(delay)
        except TelegramError as e:
            if "poll has already been closed" in str(e).lower():
                logger.info(
                    "Опрос уже закрыт автоматически (close_date), пробуем получить результаты.",
                )
                poll = await _get_poll_from_message(bot, current)
                return poll
            if attempt < SEND_POLL_MAX_ATTEMPTS and _is_retryable_api_error(e):
                delay = SEND_POLL_RETRY_DELAY_SECONDS * attempt
                logger.warning(
                    "stop_poll: временная ошибка (%s). Повтор через %s сек (%s/%s).",
                    e,
                    delay,
                    attempt + 1,
                    SEND_POLL_MAX_ATTEMPTS,
                )
                await asyncio.sleep(delay)
                continue
            logger.error("stop_poll: %s", e)
            poll = await _get_poll_from_message(bot, current)
            return poll

    logger.warning(
        "stop_poll: исчерпаны попытки (%s), пробуем получить опрос через forward.",
        SEND_POLL_MAX_ATTEMPTS,
    )
    return await _get_poll_from_message(bot, current)


async def _send_summary_resilient(bot: Bot, text: str) -> bool:
    """Отправка итогов с ретраями (чтобы не потерять из‑за Timed out)."""
    for attempt in range(1, SEND_POLL_MAX_ATTEMPTS + 1):
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=text)
            return True
        except RetryAfter as e:
            if attempt >= SEND_POLL_MAX_ATTEMPTS:
                logger.error("send_message (итоги): превышен лимит: %s", e)
                return False
            delay = max(int(getattr(e, "retry_after", 0)), SEND_POLL_RETRY_DELAY_SECONDS)
            logger.warning(
                "send_message (итоги): RetryAfter, ждём %s сек (%s/%s).",
                delay,
                attempt + 1,
                SEND_POLL_MAX_ATTEMPTS,
            )
            await asyncio.sleep(delay)
        except TelegramError as e:
            if attempt >= SEND_POLL_MAX_ATTEMPTS or not _is_retryable_api_error(e):
                logger.error("send_message (итоги): %s", e)
                return False
            delay = SEND_POLL_RETRY_DELAY_SECONDS * attempt
            logger.warning(
                "send_message (итоги): временная ошибка, повтор через %s сек: %s",
                delay,
                e,
            )
            await asyncio.sleep(delay)
    return False


async def close_poll(bot: Bot):
    """Останавливает опрос, собирает результаты и отправляет итоги."""
    history = load_history()
    current = history.get("current_poll")

    if not current:
        logger.warning("Нет активного опроса для закрытия.")
        return

    poll = await _stop_poll_resilient(bot, current)
    if poll is None:
        logger.error("Не удалось закрыть опрос и получить данные для итогов.")
        return

    try:
        voter_counts = [opt.voter_count for opt in poll.options]
        num_standard = len(POLL_OPTIONS)
        custom_options = []
        for i, opt in enumerate(poll.options):
            if i >= num_standard:
                custom_options.append({
                    "text": opt.text,
                    "votes": opt.voter_count,
                })

        results = compute_results(voter_counts)
        results["custom_options"] = custom_options

        poll_number = len(history.get("polls", [])) + 1
        record = {
            "poll_number": poll_number,
            "date": current["date"],
            "greeting": current["greeting"],
            "average": results["average"],
            "hangover_avg": results["hangover_avg"],
            "hangover_count": results["hangover_count"],
            "sober_count": results["sober_count"],
            "total_voters": results["total_voters"],
            "phantom_count": results["phantom_count"],
            "still_drunk": results["still_drunk"],
            "voter_counts": voter_counts[:num_standard],
            "custom_options": custom_options,
        }

        summary = format_summary(results, history)

        if not await _send_summary_resilient(bot, summary):
            logger.error(
                "Итоги в чат не доставлены после ретраев; "
                "current_poll оставлен — повтор при следующем запуске закрытия.",
            )
            return

        history["polls"].append(record)
        history["current_poll"] = None
        save_history(history)
        logger.info("Опрос закрыт, среднее: %s", results["average"])

    except Exception as e:
        logger.error("Ошибка обработки результатов: %s", e)


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
