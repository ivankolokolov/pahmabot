"""Итоги опроса: базовые цифры, пахмограф, факты недели и вердикт.

Только чистые функции над записями history.json — без Telegram, чтобы отчёт можно было
тестировать и смотреть превью на реальной истории (tools/preview_report.py).
"""

from __future__ import annotations

import random
import statistics
from datetime import datetime

from config import (
    FACT_TEXTS,
    IDX_PHANTOM,
    IDX_SOBER,
    IDX_STILL_DRUNK,
    OPTION_VALUES,
    POLL_OPTIONS,
    SUMMARY_HEADERS,
    VERDICT_TEXTS,
)

SPARK_BARS = "▁▂▃▄▅▆▇█"
SPARK_WEEKS = 8
NORM_WEEKS = 12        # окно «нормы чата» для вердикта
MIN_HISTORY = 4        # сколько прошлых недель нужно, чтобы сравнивать
NORM_DELTA = 0.3       # отличие от нормы, после которого неделя «легче/тяжелее обычного»
MAX_FACTS = 3
MAX_PERSONAL_FACTS = 2
RECORD_WEIGHT = 6      # факты с таким весом (рекорды) показываем, даже если вид был на прошлой неделе

MONTHS_GEN = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_PREP = ["", "январе", "феврале", "марте", "апреле", "мае", "июне", "июле",
               "августе", "сентябре", "октябре", "ноябре", "декабре"]


def format_summary(record: dict, history: dict, month_end: bool = False) -> str:
    """Формирует текст итогов по записи опроса.

    history — состояние до этого опроса; в нём же обновляется ротация фактов и вердиктов.
    month_end — последний опрос месяца: добавить итоги месяца.
    """
    polls = _scale_polls(history.get("polls", []))

    lines = [f"{random.choice(SUMMARY_HEADERS)} (#{record['poll_number']})"]
    lines += _base_lines(record)

    spark_values = [p["average"] for p in polls[-(SPARK_WEEKS - 1):]] + [record["average"]]
    if len(spark_values) >= MIN_HISTORY:
        lines.append(f"Пахмограф за {len(spark_values)} {_weeks(len(spark_values))}: {_sparkline(spark_values)}")

    personal = _personal_facts(record, history)
    aggregate = _aggregate_facts(record, polls)
    if any(kind == "person_top" for kind, *_ in personal):
        aggregate = [f for f in aggregate if f[0] != "heavy_top"]
    chosen = _pick_facts(aggregate + personal, history)
    if chosen:
        lines.append("")
        lines += [text for _, text in chosen]

    verdict = _verdict(record, polls, history, {kind for kind, _ in chosen})
    if verdict:
        lines += ["", verdict]

    lines += _custom_lines(record)
    if month_end:
        lines += _month_lines(record, history)
    lines += _jubilee_lines(record, history)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Базовые цифры
# ---------------------------------------------------------------------------

def _base_lines(r: dict) -> list[str]:
    total = r["total_voters"]
    first = [f"{total} {_votes(total)}"]
    if total:
        first.append(f"трезвых {r['sober_count']} ({round(r['sober_count'] / total * 100)}%)")
        with_pahma = r["hangover_count"] + r["still_drunk"]
        if with_pahma:
            first.append(f"с пахмой {with_pahma}")

    second = [f"Средняя {r['average']}"]
    if r["hangover_count"]:
        second.append(f"у похмельных {r['hangover_avg']}")
    if r["still_drunk"]:
        second.append(f"🍻 {r['still_drunk']}")
    if r["phantom_count"]:
        second.append(f"👻 {r['phantom_count']}")
    return [" · ".join(first), " · ".join(second)]


def _sparkline(values: list[float]) -> str:
    lo, hi = min(values), max(values)
    if hi == lo:
        return SPARK_BARS[3] * len(values)
    top = len(SPARK_BARS) - 1
    return "".join(SPARK_BARS[round((v - lo) / (hi - lo) * top)] for v in values)


# ---------------------------------------------------------------------------
# Факты недели. Факт — (вид, вес, текст, персональный ли); вес — насколько интересно.
# ---------------------------------------------------------------------------

def _fact(kind: str, weight: float, personal: bool = False, **kw) -> tuple:
    return kind, weight, random.choice(FACT_TEXTS[kind]).format(**kw), personal


def _aggregate_facts(r: dict, polls: list[dict]) -> list[tuple]:
    """Факты по всему чату. polls — прошлые опросы текущей шкалы."""
    total = r["total_voters"]
    counts = r["voter_counts"]
    avg = r["average"]
    facts = []

    top_idx = max(range(len(counts)), key=counts.__getitem__)
    if total:
        facts.append(_fact("popular", 1, label=_label(top_idx), count=counts[top_idx], total=total))

    if len(polls) < MIN_HISTORY:
        return facts

    totals = [p["total_voters"] for p in polls]
    if total > max(totals):
        facts.append(_fact("turnout_high", 7, n=total, votes=_votes(total)))
    elif total < min(totals):
        facts.append(_fact("turnout_low", 6, n=total, votes=_votes(total)))

    if total:
        share = r["sober_count"] / total
        shares = [p["sober_count"] / p["total_voters"] for p in polls if p["total_voters"]]
        pct = round(share * 100)
        if share > max(shares):
            facts.append(_fact("sober_share_high", 7, pct=pct))
        elif share < min(shares):
            facts.append(_fact("sober_share_low", 7, pct=pct))
        else:
            # Сколько недель подряд трезвые были «по другую сторону» половины
            k = _run_length(shares, lambda s: (s < 0.5) != (share < 0.5))
            if k >= 3:
                kind = "sober_minority" if share < 0.5 else "sober_majority"
                facts.append(_fact(kind, 5, k=k, weeks=_weeks(k)))

    avgs = [p["average"] for p in polls]
    if avg > max(avgs):
        facts.append(_fact("avg_high", 9, avg=avg))
    elif avg < min(avgs):
        facts.append(_fact("avg_low", 8, avg=avg))
    elif avg not in avgs:  # при ничьей место в рейтинге ничего не говорит
        heavier_than = 1 + sum(a > avg for a in avgs)
        lighter_than = 1 + sum(a < avg for a in avgs)
        if heavier_than <= 3:
            facts.append(_fact("rank_heavy", 4, k=heavier_than, n=len(avgs) + 1))
        elif lighter_than <= 3:
            facts.append(_fact("rank_light", 3, k=lighter_than, n=len(avgs) + 1))

    diff = round(avg - avgs[-1], 1)
    if abs(diff) >= 0.4:
        facts.append(_fact("avg_up" if diff > 0 else "avg_down", 3, prev=avgs[-1], avg=avg))

    top = _top_numeric_option(counts)
    if top is not None and OPTION_VALUES[top] >= 6:
        facts.append(_fact("heavy_top", 4 + OPTION_VALUES[top] - 6, label=_label(top),
                           count=counts[top], people=_people(counts[top])))
    elif top is not None and OPTION_VALUES[top] <= 2 and total >= 10:
        facts.append(_fact("mild_top", 3, label=_label(top)))

    if r["phantom_count"]:
        k = 1 + _run_length([p["phantom_count"] for p in polls], lambda c: c > 0)
        if k >= 4:
            facts.append(_fact("phantom_streak", 2.5, k=k))

    if r["still_drunk"]:
        k = _run_length([p["still_drunk"] for p in polls], lambda c: c == 0)
        if k >= 4:
            facts.append(_fact("drunk_back", 4, k=k, weeks=_weeks(k)))

    if avg < 1:
        k = 1 + _run_length(avgs, lambda a: a < 1)
        if k >= 3:
            facts.append(_fact("calm_streak", 2.5, k=k))

    return facts


def _personal_facts(r: dict, history: dict) -> list[tuple]:
    """Факты по конкретным людям — только если голоса этой недели собраны полностью."""
    if not r.get("votes_complete"):
        return []
    votes = r["votes"]
    people = history.get("people", {})
    polls = history.get("polls", [])
    # Непрерывная цепочка прошлых недель с полными голосами — на ней считаются серии
    chain = []
    for p in reversed(polls):
        if not p.get("votes_complete"):
            break
        chain.insert(0, p["votes"])

    def names(uids):
        return _join_names([people.get(uid, "без имени") for uid in uids])

    facts = []

    numeric = {uid: OPTION_VALUES[o] for uid, o in votes.items()
               if o in OPTION_VALUES and o != IDX_STILL_DRUNK}
    if numeric:
        top = max(numeric.values())
        holders = [uid for uid, v in numeric.items() if v == top]
        if top >= 5 and len(holders) <= 3:
            facts.append(_fact("person_top", 4 + top - 5, True,
                               label=_label(votes[holders[0]]), names=names(holders)))

    sober = {uid: 1 + _person_run(chain, uid, lambda o: o == IDX_SOBER)
             for uid, o in votes.items() if o == IDX_SOBER}
    if sober:
        best = max(sober.values())
        if best >= 3:
            holders = [uid for uid, k in sober.items() if k == best]
            facts.append(_fact("person_sober_streak", 3 + min(best, 10) * 0.3, True,
                               names=names(holders), k=best, weeks=_weeks(best)))

    same = {uid: 1 + _person_run(chain, uid, lambda x, o=o: x == o)
            for uid, o in votes.items() if o != IDX_SOBER and o < IDX_PHANTOM}
    if same:
        uid = max(same, key=same.get)
        if same[uid] >= 3:
            facts.append(_fact("person_same_streak", 4, True, name=names([uid]),
                               k=same[uid], label=_label(votes[uid])))

    jumps = {}
    for uid, o in votes.items():
        if o not in OPTION_VALUES:
            continue
        past = [OPTION_VALUES[p[uid]] for p in chain[-8:] if uid in p and p[uid] in OPTION_VALUES]
        if len(past) >= 3 and OPTION_VALUES[o] - statistics.median(past) >= 3:
            jumps[uid] = (OPTION_VALUES[o] - statistics.median(past), past)
    if jumps:
        uid = max(jumps, key=lambda u: jumps[u][0])
        jump, past = jumps[uid]
        facts.append(_fact("person_jump", 5 + min(jump, 6) * 0.3, True, name=names([uid]),
                           label=_label(votes[uid]), usual=_usual(past)))

    ends = {uid: _person_run(chain, uid, lambda x: x == IDX_SOBER)
            for uid, o in votes.items() if o != IDX_SOBER}
    if ends:
        uid = max(ends, key=ends.get)
        if ends[uid] >= 4:
            facts.append(_fact("person_streak_end", 4, True, name=names([uid]),
                               k=ends[uid], weeks=_weeks(ends[uid])))

    # Новички и вернувшиеся — только когда история по людям уже накопилась,
    # иначе в первые недели «новичками» окажутся все
    if len(chain) >= MIN_HISTORY:
        seen = set().union(*(p["votes"] for p in polls if "votes" in p))
        new = [uid for uid in votes if uid not in seen]
        if new:
            facts.append(_fact("newcomers", 4, True, names=names(new)))
        gaps = {uid: _run_length(chain, lambda p, uid=uid: uid not in p)
                for uid in votes if uid in seen}
        if gaps:
            uid = max(gaps, key=gaps.get)
            if gaps[uid] >= 3:
                facts.append(_fact("returned", 4, True, name=names([uid]),
                                   k=gaps[uid], weeks=_weeks(gaps[uid])))

    return facts


def _pick_facts(facts: list[tuple], history: dict) -> list[tuple[str, str]]:
    """Выбирает самые интересные факты.

    Виды, показанные на прошлой неделе, пропускаются (кроме рекордов), чтобы одна и та же
    серия не повторялась каждую неделю.
    """
    last = set(history.get("last_fact_kinds", []))
    ranked = sorted(facts, key=lambda f: f[1] + random.uniform(0, 0.5), reverse=True)
    chosen = []
    personal = 0
    for kind, weight, text, is_personal in ranked:
        if len(chosen) == MAX_FACTS:
            break
        if is_personal and personal == MAX_PERSONAL_FACTS:
            continue
        if kind in last and weight < RECORD_WEIGHT:
            continue
        if chosen and weight < 2:  # запасной факт — только если больше сказать нечего
            continue
        chosen.append((kind, text))
        personal += is_personal
    history["last_fact_kinds"] = [kind for kind, _ in chosen]
    return chosen


# ---------------------------------------------------------------------------
# Вердикт
# ---------------------------------------------------------------------------

def _verdict(r: dict, polls: list[dict], history: dict, shown: set[str]) -> str | None:
    window = [p["average"] for p in polls[-NORM_WEEKS:]]
    if len(window) < MIN_HISTORY:
        return None
    avg = r["average"]
    delta = round(avg - statistics.median(window), 1)
    # Рекорд за всё время уже назван фактом — не повторяем его вердиктом
    if avg < min(window) and "avg_low" not in shown:
        tier = "lightest"
    elif avg > max(window) and "avg_high" not in shown:
        tier = "heaviest"
    elif delta <= -NORM_DELTA:
        tier = "lighter"
    elif delta >= NORM_DELTA:
        tier = "heavier"
    else:
        tier = "normal"
    return _rotate(tier, history).format(n=len(window), weeks=_weeks(len(window)), delta=abs(delta))


def _rotate(tier: str, history: dict) -> str:
    """Вариант вердикта без повторов, пока не переберём все (как с приветствиями)."""
    options = VERDICT_TEXTS[tier]
    used = history.setdefault("used_verdicts", {}).setdefault(tier, [])
    available = [i for i in range(len(options)) if i not in used]
    if not available:
        used.clear()
        available = list(range(len(options)))
    idx = random.choice(available)
    used.append(idx)
    return options[idx]


# ---------------------------------------------------------------------------
# Дополнительные блоки
# ---------------------------------------------------------------------------

def _custom_lines(r: dict) -> list[str]:
    custom = r.get("custom_options", [])
    voted = [c for c in custom if c["votes"] > 0]
    if voted:
        return ["", "✏️ Народное творчество:"] + [f'  • «{c["text"]}» — {c["votes"]} гол.' for c in voted]
    if custom:
        return ["", "✏️ Народное творчество было, но никто не проголосовал."]
    return []


def _month_lines(r: dict, history: dict) -> list[str]:
    """Итоги месяца — в последнем опросе месяца."""
    polls = _scale_polls(history.get("polls", [])) + [r]
    month = _month(r)
    in_month = [p for p in polls if _month(p) == month]
    if len(in_month) < 2:
        return []

    avg = round(statistics.mean(p["average"] for p in in_month), 1)
    head = f"🗓 Итоги {MONTHS_GEN[month[1]]}: средняя {avg}"
    earlier = [p for p in polls if _month(p) < month]
    if earlier:
        prev_month = _month(earlier[-1])
        prev_avg = round(statistics.mean(p["average"] for p in earlier if _month(p) == prev_month), 1)
        head += f", в {MONTHS_PREP[prev_month[1]]} — {prev_avg}"
    lines = ["", head + "."]

    weeks = [p["votes"] for p in in_month if p.get("votes_complete")]
    if len(weeks) < 2:
        return lines
    people = history.get("people", {})
    per_person = {}
    for votes in weeks:
        for uid, o in votes.items():
            per_person.setdefault(uid, []).append(o)

    means = {uid: statistics.mean(OPTION_VALUES[o] for o in opts if o in OPTION_VALUES)
             for uid, opts in per_person.items()
             if len(opts) >= 2 and any(o in OPTION_VALUES for o in opts)}
    if means and max(means.values()) > 0:
        top = max(means.values())
        leaders = [uid for uid, m in means.items() if m == top]
        if len(leaders) <= 3:
            names = _join_names([people.get(uid, "без имени") for uid in leaders])
            lines.append(f"• Пахмист месяца — {names}: в среднем {round(top, 1)}.")

    always_sober = [uid for uid, opts in per_person.items()
                    if len(opts) == len(weeks) and all(o == IDX_SOBER for o in opts)]
    if 0 < len(always_sober) <= 3:
        names = _join_names([people.get(uid, "без имени") for uid in always_sober])
        lines.append(f"• Все недели месяца 0/10 — {names}.")
    elif always_sober:
        lines.append(f"• Все недели месяца 0/10 — {len(always_sober)} {_people(len(always_sober))}.")

    if len(weeks) >= 3:
        # Один и тот же ненулевой ответ все недели месяца; берём самую многочисленную группу
        stable = {}
        for uid, opts in per_person.items():
            if len(opts) == len(weeks) and len(set(opts)) == 1 and IDX_SOBER < opts[0] < IDX_PHANTOM:
                stable.setdefault(opts[0], []).append(uid)
        if stable:
            option, uids = max(stable.items(), key=lambda kv: len(kv[1]))
            if len(uids) <= 3:
                names = _join_names([people.get(uid, "без имени") for uid in uids])
                lines.append(f"• Каждую неделю {_label(option)} — {names}.")
    return lines


def _jubilee_lines(r: dict, history: dict) -> list[str]:
    """Историческая статистика — каждые 10 опросов (по текущей шкале, как и рекорды)."""
    n = r["poll_number"]
    if n % 10 != 0:
        return []
    polls = _scale_polls(history.get("polls", [])) + [r]
    avgs = [p["average"] for p in polls]
    since = datetime.fromisoformat(polls[0]["date"]).strftime("%d.%m.%Y")
    return ["", f"📈 Это {n}-й опрос. С {since}: средняя {round(statistics.mean(avgs), 1)}, "
                f"макс. {max(avgs)}, мин. {min(avgs)}."]


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------

def _scale_polls(polls: list[dict]) -> list[dict]:
    """Опросы текущей шкалы (с №11). У первых десяти шкала другая — сравнивать с ними нельзя."""
    return [p for p in polls if "sober_count" in p]


def _month(p: dict) -> tuple[int, int]:
    d = datetime.fromisoformat(p["date"])
    return d.year, d.month


def _run_length(seq: list, pred) -> int:
    """Сколько последних элементов подряд удовлетворяют условию."""
    k = 0
    for item in reversed(seq):
        if not pred(item):
            break
        k += 1
    return k


def _person_run(chain: list[dict], uid: str, pred) -> int:
    """Сколько последних недель подряд человек голосовал и его голос удовлетворял условию."""
    return _run_length(chain, lambda votes: uid in votes and pred(votes[uid]))


def _top_numeric_option(counts: list[int]) -> int | None:
    """Самый высокий балл недели (0–8, без «ещё пью» и фантомной)."""
    scored = [i for i in range(IDX_STILL_DRUNK) if counts[i] > 0]
    return max(scored) if scored else None


def _label(idx: int) -> str:
    """Короткая подпись варианта: «0/10», «8–9/10», «ещё пью»."""
    if idx == IDX_SOBER:
        return "0/10"
    if idx < IDX_STILL_DRUNK:
        return POLL_OPTIONS[idx].split(" — ")[0]
    if idx == IDX_STILL_DRUNK:
        return "«ещё пью»"
    if idx == IDX_PHANTOM:
        return "фантомная пахма"
    return "свой вариант"


def _usual(values: list[float]) -> str:
    lo, hi = min(values), max(values)
    if lo == hi:
        return f"{_num(lo)}/10"
    if hi - lo <= 2:
        return f"{_num(lo)}–{_num(hi)}"
    return f"около {_num(statistics.median(values))}"


def _num(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


def _join_names(names: list[str]) -> str:
    if len(names) > 3:
        return f"{', '.join(names[:3])} и ещё {len(names) - 3}"
    if len(names) > 1:
        return f"{', '.join(names[:-1])} и {names[-1]}"
    return names[0]


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


def _weeks(n: int) -> str:
    """«1 неделю / 3 недели / 5 недель» — для «за N …», «N … подряд»."""
    return _plural(n, "неделю", "недели", "недель")


def _votes(n: int) -> str:
    return _plural(n, "голос", "голоса", "голосов")


def _people(n: int) -> str:
    return _plural(n, "человек", "человека", "человек")
