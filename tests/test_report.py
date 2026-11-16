"""Текст итогов: базовые цифры, факты, вердикт, персональные номинации."""

import random
import re

import report
from config import FACT_TEXTS, REMINDER_TEXTS, VERDICT_TEXTS


def rec(n, avg=1.0, date="2026-09-07", total=30, sober=15, hangover=13, drunk=1, phantom=1,
        counts=None, votes=None, complete=None):
    """Запись опроса как в history.json (текущая шкала)."""
    r = {
        "poll_number": n, "date": f"{date}T09:00:00+03:00", "greeting": "g",
        "average": avg, "hangover_avg": 2.0, "hangover_count": hangover, "sober_count": sober,
        "total_voters": total, "phantom_count": phantom, "still_drunk": drunk,
        "voter_counts": counts or [sober, 6, 4, 2, 1, 0, 0, 0, 0, drunk, phantom],
        "custom_options": [],
    }
    if votes is not None:
        r["votes"] = votes
        r["votes_complete"] = True if complete is None else complete
    return r


def weeks(avgs, **kw):
    return [rec(i + 1, avg=a, **kw) for i, a in enumerate(avgs)]


def facts_of(text):
    """Строки фактов — между базовыми цифрами и вердиктом."""
    blocks = text.split("\n\n")
    return blocks[1].split("\n") if len(blocks) > 2 else []


def test_plural():
    assert [report._weeks(n) for n in (1, 3, 5, 11, 21, 22)] == \
        ["неделю", "недели", "недель", "недель", "неделю", "недели"]
    assert report._votes(31) == "голос" and report._votes(24) == "голоса" and report._votes(35) == "голосов"


def test_sparkline():
    assert report._sparkline([0.5, 1.0, 1.5]) == "▁▅█"
    assert report._sparkline([1.0, 1.0]) == "▄▄"


def test_starts_with_header_not_reveal_phrase():
    text = report.format_summary(rec(5), {"polls": weeks([1.0] * 4)})
    assert text.startswith("📊") and "(#5)" in text.split("\n")[0]
    assert "🔓" not in text


def test_cold_start_has_no_comparisons():
    text = report.format_summary(rec(3), {"polls": weeks([1.0, 1.0])})
    assert "Пахмограф" not in text
    assert not any(line in text for options in VERDICT_TEXTS.values() for line in options)


def old_layout(avg, counts):
    """Запись февраля–апреля: «Ещё пью» первым вариантом, без разбивки по трезвым."""
    return {"date": "2026-03-16T09:00:00+03:00", "greeting": "g", "average": avg,
            "total_voters": sum(counts), "still_drunk": counts[0], "phantom_count": counts[10],
            "voter_counts": counts}


def test_old_layout_is_converted():
    p = report._from_old_layout(old_layout(1.8, [3, 19, 3, 3, 2, 2, 1, 0, 0, 0, 1]))
    assert p["voter_counts"] == [19, 3, 3, 2, 2, 1, 0, 0, 0, 3, 1]
    assert (p["average"], p["sober_count"], p["still_drunk"], p["phantom_count"]) == (1.8, 19, 3, 1)


def test_old_layout_polls_count_for_records():
    polls = [old_layout(1.8, [3, 19, 3, 3, 2, 2, 1, 0, 0, 0, 1])] + weeks([1.0, 1.2, 0.9, 1.1])
    random.seed(1)
    assert "рекорд" not in report.format_summary(rec(6, avg=1.7), {"polls": polls}).lower()
    random.seed(1)
    text = report.format_summary(rec(6, avg=1.9), {"polls": polls}).lower()
    assert "рекорд" in text or "самая высокая средняя" in text


def tier_of(text):
    for tier, options in VERDICT_TEXTS.items():
        for option in options:
            pattern = re.escape(option).replace(r"\{n\}", r"\d+").replace(r"\{weeks\}", r"\w+") \
                .replace(r"\{delta\}", r"[\d.]+")
            if re.fullmatch(pattern, text):
                return tier
    return None


def test_verdict_tiers():
    polls = weeks([0.8, 1.2, 1.0, 0.9, 1.1, 1.0])
    assert tier_of(report._verdict(rec(7, avg=1.0), polls, {}, set())) == "normal"
    assert tier_of(report._verdict(rec(7, avg=1.2), polls, {}, set())) == "normal"
    assert tier_of(report._verdict(rec(7, avg=0.7), polls, {}, set())) == "lightest"
    assert tier_of(report._verdict(rec(7, avg=0.7), polls, {}, {"avg_low"})) == "lighter"
    assert tier_of(report._verdict(rec(7, avg=1.5), polls, {}, set())) == "heaviest"
    polls_wide = weeks([0.5, 1.8, 1.0, 1.0, 1.0, 1.0])
    assert tier_of(report._verdict(rec(7, avg=1.4), polls_wide, {}, set())) == "heavier"


def test_verdict_does_not_repeat_until_exhausted():
    polls = weeks([1.0] * 6)
    history = {}
    seen = [report._verdict(rec(7), polls, history, set()) for _ in VERDICT_TEXTS["normal"]]
    assert len(set(seen)) == len(VERDICT_TEXTS["normal"])


def test_last_week_kind_is_skipped_but_records_are_not():
    polls = weeks([1.0, 1.1, 0.9, 1.0, 1.0], phantom=1)
    random.seed(0)
    text = report.format_summary(rec(6, phantom=1), {"polls": polls, "last_fact_kinds": ["phantom_streak"]})
    assert "Фантомная пахма" not in "\n".join(facts_of(text))
    text = report.format_summary(rec(6, avg=2.0), {"polls": polls, "last_fact_kinds": ["avg_high"]})
    assert "2.0" in "\n".join(facts_of(text))


PEOPLE = {"1": "Маша", "2": "Петя", "3": "Коля", "4": "Саша"}


def test_personal_sober_streak():
    history = {"people": PEOPLE, "polls": [
        rec(i + 1, votes={"1": 0, "2": 1}) for i in range(3)]}
    text = report.format_summary(rec(4, votes={"1": 0, "2": 2}), history)
    assert '<a href="tg://user?id=1">Маша</a> — 4 недели подряд 0/10.' in text
    assert "Маша — 4 недели подряд 0/10." in report.plain_text(text)


def test_personal_facts_need_complete_votes():
    history = {"people": PEOPLE, "polls": [
        rec(i + 1, votes={"1": 0}) for i in range(3)]}
    text = report.format_summary(rec(4, votes={"1": 0}, complete=False), history)
    assert "Маша" not in text


def test_person_top_replaces_anonymous_max():
    counts = [15, 6, 4, 2, 1, 0, 0, 1, 0, 1, 1]
    history = {"people": PEOPLE, "polls": weeks([1.0] * 4)}
    random.seed(3)
    text = report.plain_text(report.format_summary(rec(5, counts=counts, votes={"2": 7, "1": 0}), history))
    assert "Максимум недели — 7/10: Петя." in text
    assert "(1 человек)" not in text


def test_person_jump():
    history = {"people": PEOPLE, "polls": [
        rec(i + 1, votes={"3": o}) for i, o in enumerate([0, 0, 1])]}
    text = report.plain_text(report.format_summary(rec(4, votes={"3": 5}), history))
    assert "Коля — 5/10, хотя обычно 0–1." in text


def test_newcomers_only_after_history_accumulates():
    early = {"people": PEOPLE, "polls": [rec(i + 1, votes={"1": 0}) for i in range(2)]}
    assert "Первый голос" not in report.format_summary(rec(3, votes={"1": 0, "4": 1}), early)

    later = {"people": PEOPLE, "polls": [rec(i + 1, votes={"1": 0}) for i in range(4)]}
    random.seed(0)
    assert "Первый голос в опросе: Саша." in report.plain_text(
        report.format_summary(rec(5, votes={"1": 0, "4": 1}), later))


def test_month_block():
    history = {"people": PEOPLE, "polls": [
        rec(1, avg=1.2, date="2026-08-31", votes={"1": 0, "2": 3}),
        rec(2, avg=0.8, date="2026-09-07", votes={"1": 0, "2": 3}),
        rec(3, avg=1.0, date="2026-09-14", votes={"1": 0, "2": 3}),
        rec(4, avg=0.9, date="2026-09-21", votes={"1": 0, "2": 2}),
    ]}
    text = report.format_summary(rec(5, avg=1.1, date="2026-09-28", votes={"1": 0, "2": 3}),
                                 history, month_end=True)
    block = month_block(text)
    assert block[:2] == ["🗓 Итоги сентября",
                         "Средняя 1.0 (в августе 1.2) · трезвых 50% · самая тяжёлая неделя — 28.09 (1.1)"]
    assert "• Пахмист месяца — Петя: в среднем 2.8." in block
    assert "• Все недели 0/10 — Маша." in block


def test_all_templates_format():
    kw = dict(k=5, n=30, avg=1.2, prev=1.0, pct=46, label="3/10", count=2, total=30,
              name="Маша", names="Маша и Петя", usual="0–1", weeks="недель",
              votes="голосов", people="человека", delta=0.4)
    for options in list(FACT_TEXTS.values()) + list(VERDICT_TEXTS.values()):
        for template in options:
            assert "{" not in template.format(**kw)


def test_random_history_smoke():
    """Год случайных недель с голосами: отчёт строится, фактов не больше трёх, всё подставлено."""
    rng = random.Random(42)
    people = {str(i): f"Человек <{i}> & Co" for i in range(40)}
    polls = []
    history = {"people": people, "polls": polls}
    for n in range(1, 53):
        votes = {uid: rng.choice([0, 0, 0, 1, 2, 3, 5, 7, 9, 10]) for uid in people if rng.random() < 0.8}
        counts = [0] * 11
        for o in votes.values():
            counts[o] += 1
        r = rec(n, avg=round(rng.uniform(0.3, 2.0), 1), date=f"2026-{1 + n // 5:02d}-{1 + n % 5 * 5:02d}",
                total=len(votes), sober=counts[0], hangover=sum(counts[1:9]), drunk=counts[9],
                phantom=counts[10], counts=counts, votes=votes, complete=rng.random() < 0.9)
        text = report.format_summary(r, history, month_end=n % 4 == 0)
        assert "{" not in text and "}" not in text
        assert not re.search(r"<(?!/?a\b)|&(?!(lt|gt|amp);)", text)  # кроме тегов людей всё экранировано
        assert len(facts_of(text)) <= 3
        polls.append(r)


# ---------------------------------------------------------------------------
# Итоги месяца и года
# ---------------------------------------------------------------------------

OCTOBER = {
    "people": PEOPLE,
    "polls": [
        rec(1, avg=1.0, date="2026-09-21", votes={"1": 3, "2": 1, "3": 0, "4": 0}),
        rec(2, avg=1.0, date="2026-09-28", votes={"1": 2, "2": 1, "3": 0, "4": 0}),
        rec(3, avg=0.9, date="2026-10-05", votes={"1": 2, "2": 3, "3": 0, "4": 0}),
        rec(4, avg=1.6, date="2026-10-12", votes={"1": 0, "2": 4, "3": 6, "4": 0}),
        rec(5, avg=1.0, date="2026-10-19", votes={"1": 0, "2": 3, "3": 7, "4": 0}),
    ],
}


def month_block(text):
    text = report.plain_text(text)
    return text[text.index("🗓"):].split("\n")


def test_month_header():
    text = report.format_summary(rec(6, avg=0.9, date="2026-10-26", votes={"1": 0, "2": 4, "3": 0, "4": 0}),
                                 OCTOBER, month_end=True)
    block = month_block(text)
    assert block[0] == "🗓 Итоги октября"
    assert block[1] == "Средняя 1.1 (в сентябре 1.0) · трезвых 50% · самая тяжёлая неделя — 12.10 (1.6)"


def test_month_nominations_are_personal():
    text = report.format_summary(rec(6, avg=0.9, date="2026-10-26", votes={"1": 0, "2": 4, "3": 0, "4": 0}),
                                 OCTOBER, month_end=True)
    bullets = [line for line in month_block(text) if line.startswith("•")]
    assert "• Пахмист месяца — Петя: в среднем 3.5." in bullets
    assert "• Максимум месяца — 7/10: Коля (19.10)." in bullets
    assert "• Самые большие качели — Коля: от 0/10 до 7/10." in bullets
    assert "• Больше всего снизилась средняя — Маша: 2.5 → 0.5." in bullets
    assert len(bullets) <= 4


def test_month_without_votes_has_only_header():
    polls = [rec(i + 1, avg=1.0, date=d) for i, d in enumerate(["2026-10-05", "2026-10-12", "2026-10-19"])]
    text = report.format_summary(rec(4, date="2026-10-26"), {"polls": polls}, month_end=True)
    block = month_block(text)
    assert block[0] == "🗓 Итоги октября" and not any(line.startswith("•") for line in block)


YEAR = {
    "people": PEOPLE,
    "polls": [
        old_layout(0.4, [0, 24, 5, 1, 0, 1, 0, 0, 0, 0, 3]) | {"date": "2026-02-16T09:00:00+03:00"},
        old_layout(1.8, [3, 19, 3, 3, 2, 2, 1, 0, 0, 0, 1]) | {"date": "2026-03-16T09:00:00+03:00"},
        rec(3, avg=1.0, date="2026-06-15", total=39),
        rec(4, avg=1.2, date="2026-09-14", total=24),
        rec(5, avg=0.9, date="2026-12-07", votes={"1": 0, "2": 8, "3": 2, "4": 0}),
        rec(6, avg=1.1, date="2026-12-14", votes={"1": 0, "2": 3, "3": 2, "4": 1}),
        rec(7, avg=1.0, date="2026-12-21", votes={"1": 0, "2": 4, "3": 2}),
    ],
}


def test_year_summary():
    last = rec(8, avg=1.4, date="2026-12-28", votes={"1": 0, "2": 3, "3": 2, "4": 0})
    last["custom_options"] = [{"text": "Отдыхаю", "votes": 3}]
    lines = report.plain_text(report.format_year_summary(last, YEAR)).split("\n")
    assert lines[0] == "🎆 Итоги 2026 года"
    assert lines[2].startswith("8 опросов · средняя 1.1 · трезвых ")
    assert "Самая тяжёлая неделя — 16.03 (1.8), самая лёгкая — 16.02 (0.4)" in lines
    assert "Явка: максимум 39 (15.06), минимум 24 (14.09)" in lines
    assert "Лучшее народное творчество — «Отдыхаю» (3 гол., 28.12)" in lines
    assert "По людям — с 07.12, 4 опроса:" in lines
    assert "• Пахмист года — Петя: в среднем 4.6." in lines
    assert "• Больше всего недель 0/10 — Маша: 4 из 4." in lines
    assert "• Максимум года — 8–9/10: Петя (07.12)." in lines
    assert "• Самый стабильный — Коля: 2/10 4 раза из 4." in lines


# ---------------------------------------------------------------------------
# Теги людей
# ---------------------------------------------------------------------------

def test_people_are_tagged_by_id():
    assert report._mention("42", {"42": "Маша"}) == '<a href="tg://user?id=42">Маша</a>'
    assert report._mention("42", {}) == '<a href="tg://user?id=42">без имени</a>'
    assert report._mention("42", {"42": "<Вася & Co>"}) == '<a href="tg://user?id=42">&lt;Вася &amp; Co&gt;</a>'


def test_all_names_are_listed_without_and_n_more():
    people = {str(i): f"Человек {i}" for i in range(1, 7)}
    assert report._names(list(people), people).count("tg://user?id=") == 6
    assert report.plain_text(report._names(list(people), people)) == \
        "Человек 1, Человек 2, Человек 3, Человек 4, Человек 5 и Человек 6"


def test_many_newcomers_are_all_tagged():
    people = {str(i): f"Человек {i}" for i in range(1, 8)}
    history = {"people": people, "polls": [rec(i + 1, votes={"1": 0}) for i in range(4)]}
    random.seed(0)
    text = report.format_summary(rec(5, votes={uid: 0 for uid in people}), history)
    assert "и ещё" not in text
    assert "Первый голос в опросе: Человек 2, Человек 3, Человек 4, Человек 5, Человек 6 и Человек 7." \
        in report.plain_text(text)
    assert all(f'tg://user?id={i}"' in text for i in range(2, 8))


def test_custom_option_text_is_escaped():
    r = rec(5)
    r["custom_options"] = [{"text": "<b>пиво</b> & чипсы", "votes": 2}]
    text = report.format_summary(r, {"polls": weeks([1.0] * 4)})
    assert "«&lt;b&gt;пиво&lt;/b&gt; &amp; чипсы» — 2 гол." in text
    assert "«<b>пиво</b> & чипсы» — 2 гол." in report.plain_text(text)


# ---------------------------------------------------------------------------
# Напоминание за час до закрытия
# ---------------------------------------------------------------------------

def test_reminder_lists_everyone_sorted_with_link():
    people = {"1": "Яна", "2": "алекс", "3": "Борис"}
    [text] = report.format_reminder(["1", "2", "3"], people, "https://t.me/c/123/777")
    lines = text.split("\n")
    assert lines[0] in REMINDER_TEXTS
    assert report.plain_text(lines[2]) == "алекс, Борис и Яна"
    assert lines[-1] == '👉 <a href="https://t.me/c/123/777">К опросу</a>'


def test_long_reminder_is_split():
    people = {str(i): f"Человек {i:03}" for i in range(120)}
    messages = report.format_reminder(list(people), people)
    assert [m.count("tg://user") for m in messages] == [50, 50, 20]
    assert messages[0].split("\n")[0] in REMINDER_TEXTS and "К опросу" not in messages[0]
    assert report.format_reminder([], people) == []
