#!/usr/bin/env python3
"""Collect unchecked todos from daily notes of the current week into a weekly summary.
Assumes daily notes are named YYYY-MM-DD.md and contain markdown checkboxes.
Can be run as a cron job every Sunday evening or Monday morning to generate the summary for the past week."""

import re
from datetime import date, timedelta
from pathlib import Path

DAILY_NOTES_DIR = Path("/to/your/daily/Obsidian/projectname/notes")
print(f"Using daily notes directory: {DAILY_NOTES_DIR}")
SUMMARY_DIR = Path("/to/your/weekly/Obsidian/projectname/summary")

UNCHECKED_TODO_PATTERN = re.compile(r"^- \[ \] .+", re.MULTILINE)


def get_current_week_dates() -> tuple[date, date, list[date]]:
    """Return (monday, friday, [all days mon-sun]) for the current ISO week."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    days = [monday + timedelta(days=i) for i in range(7)]
    return monday, sunday, days


def collect_unchecked_todos(days: list[date]) -> dict[date, list[str]]:
    """Read daily notes for given days and return unchecked todos per day."""
    todos: dict[date, list[str]] = {}
    for day in days:
        note_path = DAILY_NOTES_DIR / f"{day.isoformat()}.md"
        if not note_path.exists():
            continue
        content = note_path.read_text(encoding="utf-8")
        matches = UNCHECKED_TODO_PATTERN.findall(content)
        if matches:
            todos[day] = matches
    return todos


def build_summary(monday: date, todos: dict[date, list[str]]) -> str:
    """Build markdown content for the weekly summary."""
    week_number = monday.isocalendar()[1]
    lines = [""]

    if not todos:
        lines.append("No unchecked todos this week.")
        return "\n".join(lines) + "\n"

    for day in sorted(todos):
        lines.append(f"### {day.strftime('%a %Y-%m-%d')}")
        lines.append("")
        for todo in todos[day]:
            lines.append(todo)
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    monday, _, days = get_current_week_dates()
    todos = collect_unchecked_todos(days)
    summary = build_summary(monday, todos)

    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    week_number = monday.isocalendar()[1]
    filename = f"W{week_number:02d} - {monday.isoformat()}.md"
    output_path = SUMMARY_DIR / filename
    output_path.write_text(summary, encoding="utf-8")
    print(f"Written summary to {output_path}")


if __name__ == "__main__":
    main()
