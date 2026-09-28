"""Текст итогов: базовые цифры, факты, вердикт, персональные номинации."""

import random
import re

import report
from config import FACT_TEXTS, VERDICT_TEXTS


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


def test_old_scale_does_not_block_records():
    old = [{"poll_number": i + 1, "date": "2026-03-02T09:00:00+03:00", "average": a}
           for i, a in enumerate([0.4, 1.8, 0.4])]
    polls = old + [rec(4 + i, avg=a) for i, a in enumerate([1.0, 1.2, 0.9, 1.1])]
    history = {"polls": polls}
    random.seed(1)
    text = report.format_summary(rec(8, avg=1.7), history)
    assert "рекорд" in text.lower() or "самая высокая средняя" in text.lower()


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
    assert "Маша — 4 недели подряд 0/10." in text


def test_personal_facts_need_complete_votes():
    history = {"people": PEOPLE, "polls": [
        rec(i + 1, votes={"1": 0}) for i in range(3)]}
    text = report.format_summary(rec(4, votes={"1": 0}, complete=False), history)
    assert "Маша" not in text


def test_person_top_replaces_anonymous_max():
    counts = [15, 6, 4, 2, 1, 0, 0, 1, 0, 1, 1]
    history = {"people": PEOPLE, "polls": weeks([1.0] * 4)}
    random.seed(3)
    text = report.format_summary(rec(5, counts=counts, votes={"2": 7, "1": 0}), history)
    assert "Максимум недели — 7/10: Петя." in text
    assert "(1 человек)" not in text


def test_person_jump():
    history = {"people": PEOPLE, "polls": [
        rec(i + 1, votes={"3": o}) for i, o in enumerate([0, 0, 1])]}
    text = report.format_summary(rec(4, votes={"3": 5}), history)
    assert "Коля — 5/10, хотя обычно 0–1." in text


def test_newcomers_only_after_history_accumulates():
    early = {"people": PEOPLE, "polls": [rec(i + 1, votes={"1": 0}) for i in range(2)]}
    assert "Первый голос" not in report.format_summary(rec(3, votes={"1": 0, "4": 1}), early)

    later = {"people": PEOPLE, "polls": [rec(i + 1, votes={"1": 0}) for i in range(4)]}
    random.seed(0)
    assert "Первый голос в опросе: Саша." in report.format_summary(rec(5, votes={"1": 0, "4": 1}), later)


def test_month_block():
    history = {"people": PEOPLE, "polls": [
        rec(1, avg=1.2, date="2026-08-31", votes={"1": 0, "2": 3}),
        rec(2, avg=0.8, date="2026-09-07", votes={"1": 0, "2": 3}),
        rec(3, avg=1.0, date="2026-09-14", votes={"1": 0, "2": 3}),
        rec(4, avg=0.9, date="2026-09-21", votes={"1": 0, "2": 2}),
    ]}
    text = report.format_summary(rec(5, avg=1.1, date="2026-09-28", votes={"1": 0, "2": 3}),
                                 history, month_end=True)
    assert "🗓 Итоги сентября: средняя 1.0, в августе — 1.2." in text
    assert "• Пахмист месяца — Петя: в среднем 2.8." in text
    assert "• Все недели месяца 0/10 — Маша." in text


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
    people = {str(i): f"Человек {i}" for i in range(40)}
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
        assert len(facts_of(text)) <= 3
        polls.append(r)
