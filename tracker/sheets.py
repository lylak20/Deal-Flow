"""4. Spreadsheet Updater: the Google Sheet is the database. Raw Database is append-only."""

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import gspread

from collector import Article
from normalize import EventKey, event_key

RAW = "Raw Database"
DASHBOARD = "Funding Dashboard"
ARTICLE_LOG = "Article Log"

RAW_HEADERS = [
    "Date", "Company", "Funding Round", "Amount Raised", "Lead Investor", "Other Investors",
    "Category", "Source", "Article URL", "Date Added",
]
LOG_HEADERS = ["Article URL", "Source", "Title", "Published", "Fetched At", "Status", "Attempts", "Error"]
DASHBOARD_ROUNDS = ["Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Series D", "Series E+"]


@dataclass
class LogEntry:
    row: int
    article: Article
    status: str
    attempts: int


def open_spreadsheet() -> gspread.Spreadsheet:
    """GOOGLE_SERVICE_ACCOUNT_JSON may hold the key JSON itself (GitHub Actions) or a path to it (local)."""
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"].strip()
    info = json.loads(raw if raw.startswith("{") else Path(raw).read_text(encoding="utf-8"))
    return gspread.service_account_from_dict(info).open_by_key(os.environ["SHEET_ID"])


def _first_row(append_response: dict) -> int:
    # e.g. "'Article Log'!A12:H14" -> 12
    rng = append_response["updates"]["updatedRange"]
    return int(re.search(r"![A-Z]+(\d+)", rng).group(1))


class SheetStore:
    def __init__(self, spreadsheet: gspread.Spreadsheet):
        self._raw = spreadsheet.worksheet(RAW)
        self._log = spreadsheet.worksheet(ARTICLE_LOG)

    def existing_events(self) -> list[EventKey]:
        rows = self._raw.get("A2:C")
        return [event_key(r[1], r[2], r[0]) for r in rows if len(r) >= 3]

    def article_log(self) -> dict[str, LogEntry]:
        entries = {}
        for i, r in enumerate(self._log.get("A2:H"), start=2):
            r = r + [""] * (len(LOG_HEADERS) - len(r))
            published = datetime.fromisoformat(r[3]) if r[3] else None
            entries[r[0]] = LogEntry(
                row=i,
                article=Article(url=r[0], source=r[1], title=r[2], summary="", published=published),
                status=r[5],
                attempts=int(r[6] or 0),
            )
        return entries

    def log_new_articles(self, articles: list[Article], fetched_at: str) -> dict[str, LogEntry]:
        """Record article metadata as 'pending' before any processing."""
        if not articles:
            return {}
        rows = [
            [a.url, a.source, a.title, a.published.isoformat() if a.published else "", fetched_at, "pending", 0, ""]
            for a in articles
        ]
        start = _first_row(self._log.append_rows(rows, value_input_option="RAW", table_range="A1"))
        return {a.url: LogEntry(row=start + i, article=a, status="pending", attempts=0) for i, a in enumerate(articles)}

    def append_events(self, rows: list[list[str]]) -> None:
        if rows:
            self._raw.append_rows(rows, value_input_option="RAW", table_range="A1")

    def update_statuses(self, updates: list[tuple[int, str, int, str]]) -> None:
        """updates: (row, status, attempts, error)."""
        if updates:
            self._log.batch_update(
                [{"range": f"F{row}:H{row}", "values": [[status, attempts, error[:500]]]}
                 for row, status, attempts, error in updates],
                value_input_option="RAW",
            )
