#!/usr/bin/env python3
"""
ПахмаБот — еженедельный понедельничный опрос о состоянии пахмы.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta

import httpx
from telegram import Bot, Poll
from telegram.error import Conflict, RetryAfter, TelegramError
from telegram.request import HTTPXRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import (
    API_MAX_ATTEMPTS,
    API_READ_TIMEOUT_SECONDS,
    API_RETRY_DELAY_SECONDS,
    BOT_TOKEN,
    CHANNEL_ID,
    CLOSE_HOUR,
    DATA_DIR,
    GREETING_MESSAGES,
    HISTORY_FILE,
    HOLIDAY_GREETINGS,
    IDX_SOBER,
    POLL_HOUR,
    POLL_OPTIONS,
    POLL_TAGLINES,
    RU_HOLIDAYS,
    SEASONAL_MESSAGES,
    TELEGRAM_PROXY,
    ZERO_OPTIONS,
)
from report import compute_results, format_summary, format_year_summary

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx на INFO пишет URL каждого запроса, а в URL — токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("pahmabot")

MSK = pytz.timezone("Europe/Moscow")


# ---------------------------------------------------------------------------
# Хранилище данных
# ---------------------------------------------------------------------------

def load_history() -> dict:
    """Загружает историю опросов из JSON-файла."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except ValueError as e:
            logger.error("Повреждён history.json, создаю резервную копию: %s", e)
            os.replace(HISTORY_FILE, HISTORY_FILE + ".bak")
    return {"polls": [], "current_poll": None}


def save_history(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
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
# Бот: запросы к Telegram API
# ---------------------------------------------------------------------------

# Сетевые ошибки, при которых запрос гарантированно не ушёл в Telegram
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


async def _call_api(what: str, request):
    """Выполняет запрос к Telegram API, повторяя его, только если он точно не выполнен.

    Повторяем RetryAfter и ошибки соединения. После таймаута чтения или обрыва
    ответа запрос мог уже выполниться (так задваивался опрос), поэтому такие
    ошибки, как и отказы API (BadRequest и т.п.), пробрасываем сразу.
    """
    for attempt in range(1, API_MAX_ATTEMPTS + 1):
        try:
            return await request()
        except TelegramError as e:
            if attempt == API_MAX_ATTEMPTS:
                raise
            if isinstance(e, RetryAfter):
                delay = max(e.retry_after, API_RETRY_DELAY_SECONDS)
            elif isinstance(e.__cause__, _NOT_SENT_ERRORS):
                delay = API_RETRY_DELAY_SECONDS * attempt
            else:
                raise
            logger.warning(
                "%s: %s. Повтор через %s сек (попытка %s/%s).",
                what, e, delay, attempt + 1, API_MAX_ATTEMPTS,
            )
            await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Бот: отправка опроса
# ---------------------------------------------------------------------------

async def maybe_send_poll(bot: Bot):
    """Отправляет опрос, если сегодня первый рабочий день недели и он ещё не отправлен."""
    today = datetime.now(MSK).date()

    first_wd = get_first_working_day_of_week(today)
    if first_wd != today:
        logger.debug(
            "Сегодня %s — не первый рабочий день недели (первый: %s), пропускаем.",
            today, first_wd,
        )
        return

    history = load_history()
    current = history.get("current_poll")
    if current:
        if datetime.fromisoformat(current["date"]).date() == today:
            logger.info("Опрос уже отправлен сегодня (message_id=%s), пропускаем.", current["message_id"])
            return
        # Прошлый опрос так и не закрылся (например, сообщение удалили) —
        # не даём ему заблокировать новые опросы навсегда.
        logger.warning(
            "Опрос от %s (message_id=%s) так и не закрыт, забываю его.",
            current["date"], current["message_id"],
        )
        history["current_poll"] = None
        save_history(history)

    logger.info("Сегодня %s — первый рабочий день недели, запускаем опрос.", today)
    await send_poll(bot)


def _build_poll_options() -> list[str]:
    """Собирает варианты ответа, подставляя случайный текст для 0/10."""
    options = list(POLL_OPTIONS)
    options[IDX_SOBER] = random.choice(ZERO_OPTIONS)
    return options


async def send_poll(bot: Bot):
    """Отправляет опрос в канал."""
    history = load_history()
    greeting = pick_greeting(history)
    options = _build_poll_options()

    tagline = random.choice(POLL_TAGLINES)
    description = (
        f"{tagline}\n\n"
        "✏️ Можешь добавить свой вариант ответа"
    )

    now = datetime.now(MSK)
    close_time = now.replace(hour=23, minute=59, second=0, microsecond=0)
    close_timestamp = int(close_time.timestamp())

    logger.info("Отправляю опрос: %s", greeting)
    try:
        message = await _call_api("sendPoll", lambda: bot.send_poll(
            chat_id=CHANNEL_ID,
            question=greeting,
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
            close_date=close_timestamp,
            api_kwargs={
                "description": description,
                "allow_adding_options": True,
            },
        ))
    except TelegramError as e:
        logger.error(
            "Ошибка отправки опроса: %s. Повторно не отправляю — "
            "если это таймаут, опрос мог дойти, проверь канал.",
            e,
        )
        return

    history["current_poll"] = {
        "message_id": message.message_id,
        "chat_id": message.chat.id,
        "date": datetime.now(MSK).isoformat(),
        "greeting": greeting,
    }
    save_history(history)
    logger.info("Опрос отправлен, message_id=%s", message.message_id)


# ---------------------------------------------------------------------------
# Бот: закрытие опроса и итоги
# ---------------------------------------------------------------------------

async def _fetch_final_poll(bot: Bot, current: dict) -> Poll | None:
    """Останавливает опрос и возвращает его итоговое состояние.

    Если stop_poll не прошёл (опрос уже закрыт по close_date, таймаут и т.п.),
    берёт состояние из пересланной копии сообщения и сразу её удаляет.
    """
    chat_id = current["chat_id"]
    message_id = current["message_id"]

    try:
        return await _call_api("stopPoll", lambda: bot.stop_poll(
            chat_id=chat_id,
            message_id=message_id,
        ))
    except TelegramError as e:
        logger.warning("stop_poll: %s — пробую получить результаты через пересылку.", e)

    try:
        fwd = await _call_api("forwardMessage", lambda: bot.forward_message(
            chat_id=chat_id,
            from_chat_id=chat_id,
            message_id=message_id,
            disable_notification=True,
        ))
    except TelegramError as e:
        logger.error("Не удалось переслать сообщение опроса: %s", e)
        return None

    try:
        await bot.delete_message(chat_id=chat_id, message_id=fwd.message_id)
    except TelegramError:
        pass
    return fwd.poll


# Доля голосов «по людям» от итога Telegram, при которой неделя считается полной.
# Меньше — значит, часть голосов не дошла (бот лежал больше суток), и персональные
# серии эту неделю пропускают. Голоса анонимных админов в людей не попадают никогда.
VOTES_COMPLETE_SHARE = 0.9


async def _collect_votes(bot: Bot, poll_id: str, history: dict) -> dict:
    """Забирает из очереди апдейтов голоса за опрос: {user_id: индекс варианта}.

    Telegram хранит апдейты сутки, а опрос открыт с утра до вечера — поэтому голоса
    можно забрать разом при закрытии. Каждая страница сохраняется в current_poll
    до следующего запроса (он подтверждает предыдущую страницу), так что повтор
    закрытия уже прочитанное не теряет. Последний голос человека побеждает.
    """
    current = history["current_poll"]
    votes = current.setdefault("votes", {})
    people = history.setdefault("people", {})
    offset = None
    try:
        while True:
            updates = await _call_api("getUpdates", lambda: bot.get_updates(
                offset=offset,
                limit=100,
                timeout=0,
                allowed_updates=["poll_answer"],
            ))
            if not updates:
                break
            for update in updates:
                answer = update.poll_answer
                if not answer or not answer.user or answer.poll_id != poll_id:
                    continue
                uid = str(answer.user.id)
                if answer.option_ids:
                    votes[uid] = answer.option_ids[0]
                    people[uid] = answer.user.full_name
                else:
                    votes.pop(uid, None)
            offset = updates[-1].update_id + 1
            save_history(history)
    except Conflict as e:
        logger.error("getUpdates: %s — запущен второй экземпляр бота или установлен вебхук.", e)
    except TelegramError as e:
        logger.error("Не удалось забрать голоса: %s", e)
    return votes


def _next_poll_date(d):
    """Дата следующего опроса — первый рабочий день одной из следующих недель."""
    for weeks in range(1, 5):
        next_poll = get_first_working_day_of_week(d + timedelta(weeks=weeks))
        if next_poll is not None:
            return next_poll
    return None


async def close_poll(bot: Bot):
    """Останавливает активный опрос (если он есть), собирает результаты и отправляет итоги."""
    history = load_history()
    current = history.get("current_poll")
    if not current:
        return

    poll = await _fetch_final_poll(bot, current)
    if poll is None:
        logger.error("Не удалось закрыть опрос и получить данные для итогов.")
        return

    votes = await _collect_votes(bot, poll.id, history)

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
        total = results["total_voters"]
        complete = total > 0 and len(votes) >= total * VOTES_COMPLETE_SHARE
        logger.info(
            "Голоса по людям: %s из %s%s", len(votes), total,
            "" if complete else " — неполные, персональные факты пропускаю",
        )

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
            "votes": votes,
            "votes_complete": complete,
        }

        poll_date = datetime.fromisoformat(current["date"]).date()
        next_poll = _next_poll_date(poll_date)
        month_end = next_poll is None or next_poll.month != poll_date.month
        year_end = next_poll is None or next_poll.year != poll_date.year
        summary = format_summary(record, history, month_end=month_end)
        year_summary = format_year_summary(record, history) if year_end else None

        try:
            await _call_api("sendMessage (итоги)", lambda: bot.send_message(
                chat_id=CHANNEL_ID,
                text=summary,
            ))
        except TelegramError as e:
            logger.error(
                "Итоги в чат не доставлены (%s); "
                "current_poll оставлен — повтор при следующем запуске закрытия.",
                e,
            )
            return

        history["polls"].append(record)
        history["current_poll"] = None
        save_history(history)
        logger.info("Опрос закрыт, среднее: %s", results["average"])

        # Итоги года — отдельным сообщением; опрос уже закрыт, так что при сбое не повторяем
        if year_summary:
            try:
                await _call_api("sendMessage (итоги года)", lambda: bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=year_summary,
                ))
            except TelegramError as e:
                logger.error("Итоги года не доставлены: %s", e)

    except Exception:
        logger.exception("Ошибка обработки результатов")


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
        close_poll,
        CronTrigger(day_of_week="mon-fri", hour=CLOSE_HOUR, minute=0, timezone=MSK),
        args=[bot],
        id="close_poll",
        name="Проверить и закрыть опрос",
        misfire_grace_time=3600,
    )

    return scheduler


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def recover_after_restart(bot: Bot):
    """Выполняет действия, пропущенные, пока бот был выключен."""
    hour = datetime.now(MSK).hour
    if hour >= CLOSE_HOUR:
        await close_poll(bot)
    elif hour >= POLL_HOUR:
        await maybe_send_poll(bot)


async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан. Создайте .env файл.")
        return
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID не задан. Создайте .env файл.")
        return

    if TELEGRAM_PROXY:
        logger.info("Использую прокси для Telegram API.")
    def make_request():
        return HTTPXRequest(read_timeout=API_READ_TIMEOUT_SECONDS, proxy=TELEGRAM_PROXY or None)

    # getUpdates (сбор голосов) ходит через отдельный пул соединений
    bot = Bot(token=BOT_TOKEN, request=make_request(), get_updates_request=make_request())

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

    # Держим процесс живым; остановка — по SIGTERM (docker stop) или Ctrl+C
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
