#!/usr/bin/env python3
"""Превью итогов: заново рендерит отчёты по опросам из history.json.

Нужно, чтобы править тексты и пороги в config.py/report.py и сразу видеть,
как выглядели бы итоги прошлых недель. Ничего не отправляет и не меняет.
Теги людей показываются просто именами; --html — как есть, с разметкой.

    python tools/preview_report.py data/history.json            # последние 5
    python tools/preview_report.py data/history.json --last 30  # больше
    python tools/preview_report.py data/history.json --year 2026 # итоги года на сейчас
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report import format_summary, format_year_summary, plain_text  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("history", help="путь к history.json")
    parser.add_argument("--last", type=int, default=5, help="сколько последних отчётов показать")
    parser.add_argument("--year", type=int, help="показать итоги года по последнему опросу этого года")
    parser.add_argument("--html", action="store_true", help="не убирать HTML-разметку тегов")
    args = parser.parse_args()
    show = (lambda text: text) if args.html else plain_text

    with open(args.history, encoding="utf-8") as f:
        data = json.load(f)
    polls = data["polls"]

    if args.year:
        last = max(i for i, p in enumerate(polls) if p["date"].startswith(str(args.year)))
        print(show(format_year_summary(polls[last], {"people": data.get("people", {}), "polls": polls[:last]})))
        return

    # Состояние ротации переносится от недели к неделе, как в работающем боте
    state = {"people": data.get("people", {})}
    reports = []
    for i, record in enumerate(polls):
        if "sober_count" not in record:  # первые опросы — в старом формате записи, их итоги не рендерим
            continue
        month_end = i + 1 < len(polls) and polls[i + 1]["date"][:7] != record["date"][:7]
        history = {**state, "polls": polls[:i]}
        reports.append(format_summary(record, history, month_end=month_end))
        state = {k: v for k, v in history.items() if k != "polls"}

    for text in reports[-args.last:]:
        print(show(text))
        print("\n" + "─" * 40 + "\n")


if __name__ == "__main__":
    main()
