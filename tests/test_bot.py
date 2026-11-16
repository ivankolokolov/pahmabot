"""Отправка/закрытие опроса и сбор голосов — на фейковом Bot, без сети."""

import asyncio
import json
import os
import tempfile
from datetime import date
from datetime import datetime as real_datetime
from types import SimpleNamespace

import httpx
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TimedOut

import bot

MONDAY = bot.MSK.localize(real_datetime(2026, 9, 28, 9, 0, 0))
TODAY_ISO = "2026-09-28T09:00:25+03:00"


class FixedDatetime(real_datetime):
    @classmethod
    def now(cls, tz=None):
        return MONDAY.astimezone(tz) if tz else MONDAY.replace(tzinfo=None)


bot.datetime = FixedDatetime
bot.API_RETRY_DELAY_SECONDS = 0


def run(coro):
    return asyncio.run(coro)


def fresh():
    d = tempfile.mkdtemp()
    bot.DATA_DIR = d
    bot.HISTORY_FILE = os.path.join(d, "history.json")


def get_history():
    with open(bot.HISTORY_FILE, encoding="utf-8") as f:
        return json.load(f)


def with_cause(err, cause_cls):
    err.__cause__ = cause_cls("boom")
    return err


def answer(update_id, uid, option, poll_id="poll-1"):
    user = SimpleNamespace(id=uid, full_name=f"User {uid}")
    options = [] if option is None else [option]
    return SimpleNamespace(update_id=update_id,
                           poll_answer=SimpleNamespace(poll_id=poll_id, user=user, option_ids=options))


def make_poll():
    counts = [3, 2, 1, 0, 0, 0, 0, 0, 0, 1, 1]  # всего 8
    return SimpleNamespace(id="poll-1",
                           options=[SimpleNamespace(text=f"o{i}", voter_count=c) for i, c in enumerate(counts)])


BOT_ID = 999
EIGHT_VOTERS = [answer(100 + i, i, o) for i, o in enumerate([0, 0, 0, 1, 1, 2, 9, 10])]


class FakeBot:
    """Каждый метод берёт следующее действие из своего сценария: исключение или «успех».

    get_updates работает как очередь Telegram: offset подтверждает всё, что до него.
    """

    def __init__(self, updates=(), non_members=None, bot_status="administrator", **scripts):
        self.queue = list(updates)
        self.non_members = non_members or {}  # user_id → "left", "kicked" или "error"
        self.bot_status = bot_status
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = {}

    def _next(self, name, kwargs):
        self.calls.setdefault(name, []).append(kwargs)
        script = self.scripts.get(name, [])
        action = script.pop(0) if script else "ok"
        if isinstance(action, BaseException):
            raise action

    async def send_poll(self, **kw):
        self._next("send_poll", kw)
        n = len(self.calls["send_poll"])
        return SimpleNamespace(message_id=1000 + n, chat=SimpleNamespace(id=-100), poll=SimpleNamespace(id="poll-1"))

    async def stop_poll(self, **kw):
        self._next("stop_poll", kw)
        return make_poll()

    async def forward_message(self, **kw):
        self._next("forward_message", kw)
        return SimpleNamespace(message_id=5000, poll=make_poll())

    async def delete_message(self, **kw):
        self._next("delete_message", kw)

    async def send_message(self, **kw):
        self._next("send_message", kw)

    async def get_me(self):
        return SimpleNamespace(id=BOT_ID)

    async def get_chat_member(self, **kw):
        """По умолчанию все в чате, бот — админ; "error" — ответ Telegram для не участника."""
        self._next("get_chat_member", kw)
        if kw["user_id"] == BOT_ID:
            return SimpleNamespace(status=self.bot_status)
        status = self.non_members.get(kw["user_id"], "member")
        if status == "error":
            raise BadRequest("Participant_id_invalid")
        return SimpleNamespace(status=status, is_member=False)

    async def get_updates(self, offset=None, limit=100, **kw):
        self._next("get_updates", {"offset": offset, "limit": limit, **kw})
        if offset is not None:
            self.queue = [u for u in self.queue if u.update_id >= offset]
        return self.queue[:limit]


def with_current_poll(date_iso=TODAY_ISO, **extra):
    bot.save_history({"polls": [], "current_poll": {
        "message_id": 777, "chat_id": -100, "date": date_iso, "greeting": "g", **extra}})


# ---------------------------------------------------------------------------
# Отправка: повторяем только то, что точно не дошло до Telegram
# ---------------------------------------------------------------------------

def test_read_timeout_is_not_retried():
    """Запрос дошёл, но ответ опоздал — повтор создал бы второй опрос (баг 14.09 и 28.09)."""
    fresh()
    b = FakeBot(send_poll=[with_cause(TimedOut(), httpx.ReadTimeout)])
    run(bot.send_poll(b))
    assert len(b.calls["send_poll"]) == 1


def test_connect_timeout_is_retried():
    fresh()
    b = FakeBot(send_poll=[with_cause(TimedOut(), httpx.ConnectTimeout)])
    run(bot.send_poll(b))
    assert len(b.calls["send_poll"]) == 2
    assert get_history()["current_poll"]["message_id"] == 1002


def test_connect_error_is_retried():
    fresh()
    b = FakeBot(send_poll=[with_cause(NetworkError("httpx.ConnectError"), httpx.ConnectError)])
    run(bot.send_poll(b))
    assert len(b.calls["send_poll"]) == 2


def test_retry_after_is_retried():
    fresh()
    b = FakeBot(send_poll=[RetryAfter(0)])
    run(bot.send_poll(b))
    assert len(b.calls["send_poll"]) == 2


def test_bad_request_is_not_retried():
    """BadRequest в PTB — наследник NetworkError, но запрос дошёл и отклонён."""
    fresh()
    b = FakeBot(send_poll=[BadRequest("Chat not found")])
    run(bot.send_poll(b))
    assert len(b.calls["send_poll"]) == 1


def test_gives_up_after_max_attempts():
    fresh()
    b = FakeBot(send_poll=[with_cause(TimedOut(), httpx.ConnectTimeout) for _ in range(10)])
    run(bot.send_poll(b))
    assert len(b.calls["send_poll"]) == bot.API_MAX_ATTEMPTS


def test_stale_poll_does_not_block_new_week():
    """Опрос прошлой недели так и не закрылся (например, его удалили) — новый всё равно уходит."""
    fresh()
    with_current_poll("2026-09-21T09:00:00+03:00")
    b = FakeBot()
    run(bot.maybe_send_poll(b))
    assert len(b.calls["send_poll"]) == 1
    assert get_history()["current_poll"]["message_id"] == 1001


def test_todays_poll_blocks_second_send():
    fresh()
    with_current_poll()
    b = FakeBot()
    run(bot.maybe_send_poll(b))
    assert "send_poll" not in b.calls


def test_recover_after_restart_sends_missed_poll():
    fresh()
    b = FakeBot()
    run(bot.recover_after_restart(b))
    assert len(b.calls["send_poll"]) == 1


# ---------------------------------------------------------------------------
# Закрытие и сбор голосов
# ---------------------------------------------------------------------------

def test_close_collects_votes_into_record():
    fresh()
    with_current_poll()
    updates = [
        answer(1, 50, 3),                       # передумал…
        answer(2, 60, 4, poll_id="other-poll"),  # чужой опрос (например, удалённый дубль)
        *EIGHT_VOTERS,
        answer(200, 50, None),                  # …и отозвал голос
    ]
    b = FakeBot(updates=updates)
    run(bot.close_poll(b))

    h = get_history()
    record = h["polls"][0]
    assert h["current_poll"] is None
    assert record["votes"] == {str(i): o for i, o in enumerate([0, 0, 0, 1, 1, 2, 9, 10])}
    assert record["votes_complete"] is True
    assert h["people"]["7"] == "User 7"
    assert b.calls["get_updates"][0]["allowed_updates"] == ["poll_answer"]
    assert b.calls["get_updates"][-1]["offset"] == 201  # всё прочитанное подтверждено
    assert len(b.calls["send_message"]) == 1


def test_votes_survive_failed_close():
    """Страница голосов сохраняется до подтверждения — повтор закрытия её не теряет."""
    fresh()
    with_current_poll()
    b = FakeBot(updates=EIGHT_VOTERS, send_message=[BadRequest("boom")])
    run(bot.close_poll(b))
    assert len(get_history()["current_poll"]["votes"]) == 8

    b2 = FakeBot(stop_poll=[BadRequest("Poll has already been closed")])  # очередь уже пуста
    run(bot.close_poll(b2))
    record = get_history()["polls"][0]
    assert len(record["votes"]) == 8 and record["votes_complete"] is True


def test_incomplete_votes_are_marked():
    fresh()
    with_current_poll()
    b = FakeBot(updates=EIGHT_VOTERS[:5])
    run(bot.close_poll(b))
    assert get_history()["polls"][0]["votes_complete"] is False


def test_conflict_does_not_block_summary():
    fresh()
    with_current_poll()
    b = FakeBot(updates=EIGHT_VOTERS, get_updates=[Conflict("terminated by other getUpdates request")])
    run(bot.close_poll(b))
    record = get_history()["polls"][0]
    assert len(b.calls["send_message"]) == 1
    assert record["votes_complete"] is False


def test_summary_read_timeout_is_not_retried():
    fresh()
    with_current_poll()
    b = FakeBot(send_message=[with_cause(TimedOut(), httpx.ReadTimeout)])
    run(bot.close_poll(b))
    assert len(b.calls["send_message"]) == 1


def test_close_already_closed_poll_uses_silent_forward():
    fresh()
    with_current_poll()
    b = FakeBot(stop_poll=[BadRequest("Poll has already been closed")])
    run(bot.close_poll(b))
    assert b.calls["forward_message"][0]["disable_notification"] is True
    assert len(b.calls["delete_message"]) == 1
    assert get_history()["current_poll"] is None


def test_close_deleted_poll_keeps_state():
    fresh()
    with_current_poll()
    b = FakeBot(
        stop_poll=[BadRequest("Message to stop not found")] * 10,
        forward_message=[BadRequest("Message to forward not found")] * 10,
    )
    run(bot.close_poll(b))
    assert "send_message" not in b.calls
    assert get_history()["current_poll"]["message_id"] == 777


def test_next_poll_date():
    assert bot._next_poll_date(date(2026, 9, 28)) == date(2026, 10, 5)
    assert bot._next_poll_date(date(2026, 9, 21)) == date(2026, 9, 28)
    assert bot._next_poll_date(date(2026, 12, 28)) == date(2027, 1, 11)  # 4–8 января праздники


def test_last_poll_of_year_sends_year_summary_separately():
    fresh()
    with_current_poll("2026-12-28T09:00:00+03:00")
    b = FakeBot(updates=EIGHT_VOTERS)
    run(bot.close_poll(b))
    texts = [c["text"] for c in b.calls["send_message"]]
    assert len(texts) == 2 and texts[1].startswith("🎆 Итоги 2026 года")
    assert all(c["parse_mode"] == "HTML" for c in b.calls["send_message"])  # теги людей — HTML-ссылками


def test_failed_year_summary_does_not_repeat_close():
    fresh()
    with_current_poll("2026-12-28T09:00:00+03:00")
    b = FakeBot(updates=EIGHT_VOTERS, send_message=["ok", BadRequest("boom")])
    run(bot.close_poll(b))
    h = get_history()
    assert h["current_poll"] is None and len(h["polls"]) == 1


# ---------------------------------------------------------------------------
# Только участники чата: опрос могли переслать
# ---------------------------------------------------------------------------

def test_send_poll_stores_poll_id():
    fresh()
    run(bot.send_poll(FakeBot()))
    assert get_history()["current_poll"]["poll_id"] == "poll-1"


def test_votes_from_outside_the_chat_are_not_counted():
    """Пересланный опрос: голоса посторонних вычитаются из итогов Telegram и из голосов по людям."""
    fresh()
    with_current_poll()
    b = FakeBot(updates=EIGHT_VOTERS, non_members={0: "error", 6: "left"})
    run(bot.close_poll(b))
    record = get_history()["polls"][0]
    assert record["voter_counts"] == [2, 2, 1, 0, 0, 0, 0, 0, 0, 0, 1]
    assert record["total_voters"] == 6 and record["sober_count"] == 2 and record["still_drunk"] == 0
    assert set(record["votes"]) == {"1", "2", "3", "4", "5", "7"}
    assert record["votes_complete"] is True


def test_membership_check_failure_keeps_vote():
    """Непонятная ошибка getChatMember по человеку не выкидывает его голос."""
    fresh()
    with_current_poll()
    b = FakeBot(updates=EIGHT_VOTERS, get_chat_member=["ok", BadRequest("Chat not found")])
    run(bot.close_poll(b))
    assert get_history()["polls"][0]["total_voters"] == 8


def test_without_admin_rights_votes_are_not_filtered():
    """Не админу Telegram отвечает Participant_id_invalid и на настоящих участников — не фильтруем."""
    fresh()
    with_current_poll()
    b = FakeBot(updates=EIGHT_VOTERS, non_members={0: "error"}, bot_status="member")
    run(bot.close_poll(b))
    record = get_history()["polls"][0]
    assert record["total_voters"] == 8 and len(record["votes"]) == 8


def test_no_reminder_without_admin_rights():
    fresh()
    b = with_reminder_state()
    b.bot_status = "member"
    run(bot.remind_poll(b))
    assert "send_message" not in b.calls
    assert "reminded" not in get_history()["current_poll"]


def with_reminder_state(queue=()):
    """Опрос сегодня, бот знает шестерых: 1 и 2 уже голосовали, 3 вышел, 4 не из чата."""
    bot.save_history({
        "polls": [],
        "people": {str(i): f"User {i}" for i in range(1, 7)},
        "current_poll": {"message_id": 777, "chat_id": -1001234567890, "date": TODAY_ISO,
                         "greeting": "g", "poll_id": "poll-1"},
    })
    return FakeBot(updates=[answer(10, 1, 0), answer(11, 2, 3), *queue], non_members={3: "left", 4: "error"})


def test_reminder_tags_chat_members_who_have_not_voted():
    fresh()
    b = with_reminder_state()
    run(bot.remind_poll(b))
    [call] = b.calls["send_message"]
    text = call["text"]
    assert 'tg://user?id=5"' in text and 'tg://user?id=6"' in text
    assert not any(f'tg://user?id={i}"' in text for i in (1, 2, 3, 4))
    assert "https://t.me/c/1234567890/777" in text
    assert call["parse_mode"] == "HTML" and call["reply_parameters"].message_id == 777
    h = get_history()
    assert h["current_poll"]["reminded"] is True
    assert h["current_poll"]["votes"] == {"1": 0, "2": 3}  # голоса сохранены — закрытие их не потеряет

    run(bot.remind_poll(b))
    assert len(b.calls["send_message"]) == 1  # второй раз не тегаем


def test_no_reminder_when_everyone_voted():
    fresh()
    b = with_reminder_state(queue=[answer(12, 5, 0), answer(13, 6, 1)])
    run(bot.remind_poll(b))
    assert "send_message" not in b.calls


def test_no_reminder_for_poll_without_id_or_from_another_day():
    fresh()
    b = FakeBot()
    with_current_poll()  # без poll_id — отправлен до обновления
    run(bot.remind_poll(b))
    with_current_poll("2026-09-21T09:00:00+03:00", poll_id="poll-1")
    run(bot.remind_poll(b))
    assert "send_message" not in b.calls and "get_updates" not in b.calls


def test_restart_in_last_hour_sends_reminder(monkeypatch):
    at_17 = bot.MSK.localize(real_datetime(2026, 9, 28, 17, 20, 0))

    class At17(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return at_17.astimezone(tz) if tz else at_17.replace(tzinfo=None)

    monkeypatch.setattr(bot, "datetime", At17)
    fresh()
    b = with_reminder_state()
    run(bot.recover_after_restart(b))
    assert "send_poll" not in b.calls and len(b.calls["send_message"]) == 1
