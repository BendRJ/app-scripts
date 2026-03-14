
"""Cleaning Plan Scheduler — auto-generate Notion schedule pages from task definitions.

This script reads cleaning task definitions from a Notion database and creates
scheduled occurrences in a separate Notion schedule database.  It uses a **6-week
rolling window** (today → today + 6 weeks) and is **idempotent**: existing schedule
entries are detected and skipped, so the script can safely run on a cron without
producing duplicates.

Workflow
--------
1. Fetch all rows from the *Task Definitions* database (frequency, weekdays,
   validity period, description).
2. Fetch all existing rows from the *Schedule* database to build a dedup index.
3. For each definition, compute concrete occurrence dates within the effective
   window ``[max(today, valid_from), min(today+6w, valid_until)]``.
4. Create a new schedule page in Notion for every occurrence that doesn't
   already exist.

Supported frequencies
---------------------
- **Daily** — every day (optionally filtered by weekdays)
- **Weekly** — on the specified weekday(s) each week
- **Biweekly** — every two weeks, anchored to the start date's ISO-week parity
- **Monthly** — first matching weekday of each month
- **Quarterly** — first matching weekday of each quarter (Jan, Apr, Jul, Oct)
- **Yearly** — first matching weekday of January each year

Environment variables
---------------------
NOTION_TOKEN : str
    Notion integration bearer token (required).
DEF_DB_ID : str
    Database ID of the task definitions database (required).
SCHED_DB_ID : str
    Database ID of the schedule database (required).
LOG_LEVEL : str, optional
    Python log level name (default: ``"INFO"``).

Usage
-----
::

    export NOTION_TOKEN="secret_..."
    export DEF_DB_ID="<definitions-database-id>"
    export SCHED_DB_ID="<schedule-database-id>"
    python cleaning_plan_schedule.py
"""

import logging
import os
import datetime as dt
import requests
from calendar import monthrange

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Notion configuration
# ---------------------------------------------------------------------------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DEF_DB_ID = os.environ["DEF_DB_ID"]
SCHED_DB_ID = os.environ["SCHED_DB_ID"]

NOTION_VERSION = "2022-06-28"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def notion_post(path: str, payload: dict) -> dict:
    logger.debug("POST https://api.notion.com/v1/%s", path)
    r = requests.post(f"https://api.notion.com/v1/{path}", headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


def query_database_all(db_id: str) -> list[dict]:
    results = []
    payload = {"page_size": 100}
    page_num = 0
    while True:
        page_num += 1
        data = notion_post(f"databases/{db_id}/query", payload)
        batch_size = len(data["results"])
        results.extend(data["results"])
        logger.debug(
            "Database %s — fetched page %d (%d results, %d total so far)",
            db_id[:8],
            page_num,
            batch_size,
            len(results),
        )
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    logger.debug("Database %s — finished: %d total results", db_id[:8], len(results))
    return results


def get_title(page: dict, prop_name: str) -> str:
    prop = page["properties"][prop_name]
    return "".join([p["plain_text"] for p in prop["title"]]).strip()


def get_select(page: dict, prop_name: str) -> str | None:
    sel = page["properties"][prop_name]["select"]
    return sel["name"] if sel else None


def get_multi_select(page: dict, prop_name: str) -> list[str]:
    ms = page["properties"][prop_name]["multi_select"]
    return [x["name"] for x in ms]


def get_date_start(page: dict, prop_name: str) -> dt.date | None:
    d = page["properties"][prop_name]["date"]
    if not d or not d["start"]:
        return None
    return dt.date.fromisoformat(d["start"][:10])


def weekday_name_to_int(name: str) -> int:
    # Python weekday: Monday=0 ... Sunday=6
    mapping = {"Mo": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    return mapping[name]


def occurrences_daily(weekdays: list[str], start: dt.date, end: dt.date) -> list[dt.date]:
    """Every day from start to end, optionally filtered by weekdays."""
    if weekdays:
        wanted = set(weekday_name_to_int(w) for w in weekdays)
    else:
        wanted = None  # no filter → all days

    out = []
    cur = start
    while cur <= end:
        if wanted is None or cur.weekday() in wanted:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def occurrences_weekly(weekdays: list[str], start: dt.date, end: dt.date) -> list[dt.date]:
    wanted = sorted(weekday_name_to_int(w) for w in weekdays)
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() in wanted:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def first_weekday_of_month(year: int, month: int, wanted_weekday: int) -> dt.date:
    # Start at the 1st of the month, walk forward to the first matching weekday
    d = dt.date(year, month, 1)
    offset = (wanted_weekday - d.weekday()) % 7
    return d + dt.timedelta(days=offset)


def month_iter(start: dt.date, end: dt.date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1


def occurrences_biweekly(weekdays: list[str], start: dt.date, end: dt.date) -> list[dt.date]:
    """Every two weeks on the specified weekday(s), anchored to the start date's ISO week."""
    if not weekdays:
        return []
    wanted = set(weekday_name_to_int(w) for w in weekdays)
    # Anchor: ISO week number of start date (even/odd determines which weeks match)
    anchor_week_parity = start.isocalendar()[1] % 2
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() in wanted and cur.isocalendar()[1] % 2 == anchor_week_parity:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def occurrences_quarterly(weekdays: list[str], start: dt.date, end: dt.date) -> list[dt.date]:
    """First matching weekday of each quarter within the date range."""
    if not weekdays:
        return []
    anchor = min(weekday_name_to_int(w) for w in weekdays)
    quarter_starts = [1, 4, 7, 10]
    out = []
    for y, m in month_iter(start, end):
        if m not in quarter_starts:
            continue
        d = first_weekday_of_month(y, m, anchor)
        if start <= d <= end:
            out.append(d)
    return out


def occurrences_yearly(weekdays: list[str], start: dt.date, end: dt.date) -> list[dt.date]:
    """First matching weekday of January each year within the date range."""
    if not weekdays:
        return []
    anchor = min(weekday_name_to_int(w) for w in weekdays)
    out = []
    for year in range(start.year, end.year + 1):
        d = first_weekday_of_month(year, 1, anchor)
        if start <= d <= end:
            out.append(d)
    return out


def occurrences_monthly_first_weekday(weekdays: list[str], start: dt.date, end: dt.date) -> list[dt.date]:
    if not weekdays:
        return []

    # If multiple weekdays are selected, choose one deterministic anchor.
    # Here: the earliest weekday in the week (Mon..Sun). Adjust if you want one per weekday.
    anchor = min(weekday_name_to_int(w) for w in weekdays)

    out = []
    for y, m in month_iter(start, end):
        d = first_weekday_of_month(y, m, anchor)
        if start <= d <= end:
            out.append(d)
    return out


def build_existing_index(schedule_pages: list[dict]) -> set[tuple[str, str]]:
    idx = set()
    skipped = 0
    for p in schedule_pages:
        rel = p["properties"]["Task Definition"]["relation"]
        d = p["properties"]["Date"]["date"]
        if not rel or not d or not d["start"]:
            skipped += 1
            continue
        definition_id = rel[0]["id"]  # assumes exactly 1 relation
        date_str = d["start"][:10]
        idx.add((definition_id, date_str))
    logger.info(
        "Built existing-schedule index: %d entries (%d pages skipped due to missing relation/date)",
        len(idx),
        skipped,
    )
    return idx


def create_schedule_page(definition_page_id: str, task_title: str, date: dt.date):
    payload = {
        "parent": {"database_id": SCHED_DB_ID},
        "properties": {
            "Task": {"title": [{"text": {"content": task_title}}]},
            "Date": {"date": {"start": date.isoformat()}},
            "Task Definition": {"relation": [{"id": definition_page_id}]},
            "Status": {"status": {"name": "To do"}},
        },
    }
    notion_post("pages", payload)
    logger.info("Created schedule page: '%s' on %s", task_title, date.isoformat())


def main():
    today = dt.date.today()
    window_end = today + dt.timedelta(weeks=6)
    logger.info("=" * 60)
    logger.info("Cleaning Plan Scheduler — run started")
    logger.info("Today: %s | Window end: %s (6 weeks)", today, window_end)
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # Fetch data from Notion
    # ------------------------------------------------------------------
    logger.info("Fetching task definitions from database %s …", DEF_DB_ID[:8])
    definitions = query_database_all(DEF_DB_ID)
    logger.info("Fetched %d task definition(s)", len(definitions))

    logger.info("Fetching existing schedule pages from database %s …", SCHED_DB_ID[:8])
    schedule_pages = query_database_all(SCHED_DB_ID)
    logger.info("Fetched %d existing schedule page(s)", len(schedule_pages))

    existing = build_existing_index(schedule_pages)

    # ------------------------------------------------------------------
    # Process each definition
    # ------------------------------------------------------------------
    total_created = 0
    total_skipped_existing = 0
    total_skipped_definitions = 0

    for d in definitions:
        definition_id = d["id"]
        freq = get_select(d, "Frequency")
        weekdays = get_multi_select(d, "Weekdays")
        task_desc = get_title(d, "Task Description")

        valid_from = get_date_start(d, "Valid From") or today
        valid_until = get_date_start(d, "Valid Until") or window_end

        start = max(today, valid_from)
        end = min(window_end, valid_until)

        logger.info("-" * 50)
        logger.info(
            "Processing: '%s' | Frequency: %s | Weekdays: %s",
            task_desc,
            freq or "(none)",
            ", ".join(weekdays) if weekdays else "(none)",
        )
        logger.debug(
            "  Valid from: %s | Valid until: %s | Effective range: %s → %s",
            valid_from,
            valid_until,
            start,
            end,
        )

        if start > end:
            logger.info("  ⏭ Skipped — effective date range is empty (start %s > end %s)", start, end)
            total_skipped_definitions += 1
            continue

        if freq == "Daily":
            dates = occurrences_daily(weekdays, start, end)
        elif freq == "Weekly":
            if not weekdays:
                logger.info("  ⏭ Skipped — no weekdays configured")
                total_skipped_definitions += 1
                continue
            dates = occurrences_weekly(weekdays, start, end)
        elif freq == "Biweekly":
            if not weekdays:
                logger.info("  ⏭ Skipped — no weekdays configured")
                total_skipped_definitions += 1
                continue
            dates = occurrences_biweekly(weekdays, start, end)
        elif freq == "Monthly":
            if not weekdays:
                logger.info("  ⏭ Skipped — no weekdays configured")
                total_skipped_definitions += 1
                continue
            dates = occurrences_monthly_first_weekday(weekdays, start, end)
        elif freq == "Quarterly":
            if not weekdays:
                logger.info("  ⏭ Skipped — no weekdays configured")
                total_skipped_definitions += 1
                continue
            dates = occurrences_quarterly(weekdays, start, end)
        elif freq == "Yearly":
            if not weekdays:
                logger.info("  ⏭ Skipped — no weekdays configured")
                total_skipped_definitions += 1
                continue
            dates = occurrences_yearly(weekdays, start, end)
        else:
            logger.info("  ⏭ Skipped — unsupported frequency '%s'", freq)
            total_skipped_definitions += 1
            continue

        logger.info("  Generated %d occurrence(s) for '%s'", len(dates), task_desc)

        created_for_task = 0
        skipped_for_task = 0
        for occ in dates:
            key = (definition_id, occ.isoformat())
            if key in existing:
                skipped_for_task += 1
                total_skipped_existing += 1
                continue
            create_schedule_page(definition_id, task_desc, occ)
            existing.add(key)
            created_for_task += 1
            total_created += 1

        logger.info(
            "  Result for '%s': %d created, %d already existed",
            task_desc,
            created_for_task,
            skipped_for_task,
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("Run complete — Summary")
    logger.info("  Definitions processed : %d", len(definitions))
    logger.info("  Definitions skipped   : %d", total_skipped_definitions)
    logger.info("  Pages created         : %d", total_created)
    logger.info("  Occurrences skipped   : %d (already existed)", total_skipped_existing)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
