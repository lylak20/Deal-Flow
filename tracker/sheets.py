"""4. Spreadsheet Updater: the Google Sheet is the database.

Raw Database only grows, except that a later, more complete report of the same round replaces its row.
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import gspread

from collector import Article
from normalize import EventRecord, event_key

RAW = "Raw Database"
DASHBOARD = "Funding Dashboard"
ARTICLE_LOG = "Article Log"
EMAIL_TEMPLATE = "Email Template"

# "Email" holds a per-row button (formula) that opens a pre-filled Gmail draft; the pipeline itself
# produces the other columns (see main.event_rows, which leaves Email out).
RAW_HEADERS = [
    "Date", "Email", "Company", "Funding Round", "Amount Raised", "Valuation", "Lead Investor",
    "Other Investors", "Category", "Source", "Article URL", "Date Added",
]
DATA_HEADERS = [h for h in RAW_HEADERS if h != "Email"]
LOG_HEADERS = ["Article URL", "Source", "Title", "Published", "Fetched At", "Status", "Attempts", "Error"]
DASHBOARD_ROUNDS = ["Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Series D", "Series E+"]

DEFAULT_SUBJECT = "Congrats on the {Round}, {Company}"
DEFAULT_BODY = """Hi {Company} team,

Congratulations on your {Round}{Amount}! I saw the news and would love to learn more about what you're building.

Would you be open to a quick intro call in the next couple of weeks?

Best,
[Your name]"""


def col(header: str) -> str:
    """Column letter of a Raw Database header (the sheet has fewer than 26 columns)."""
    return chr(ord("A") + RAW_HEADERS.index(header))


EMAIL_COL = col("Email")


def email_formula() -> str:
    """Same formula on every row: it reads its own row via ROW(), so rows can be appended without editing it.

    Placeholders in the Email Template tab: {Company}, {Round} ("recent round" if unknown),
    {Amount} (" ($66M)" or nothing if undisclosed), {Lead} ("your investors" if unknown).
    """
    c, rd, am, ld = (f"${col(h)}:${col(h)}" for h in ("Company", "Funding Round", "Amount Raised", "Lead Investor"))
    t = f"'{EMAIL_TEMPLATE}'"
    return (
        f'=LET(r, ROW(), co, INDEX({c}, r), IF(co = "", "", LET('
        f'rd, INDEX({rd}, r), am, INDEX({am}, r), ld, INDEX({ld}, r), '
        f'rnd, IF(OR(rd = "", rd = "Undisclosed"), "recent round", rd), '
        f'amt, IF(OR(am = "", am = "Undisclosed"), "", " (" & am & ")"), '
        f'lead, IF(ld = "", "your investors", ld), '
        f'fill, LAMBDA(x, SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(x, "{{Company}}", co), '
        f'"{{Round}}", rnd), "{{Amount}}", amt), "{{Lead}}", lead)), '
        f'HYPERLINK("https://mail.google.com/mail/?view=cm&fs=1&su=" & ENCODEURL(fill({t}!$B$1)) '
        f'& "&body=" & ENCODEURL(fill({t}!$B$2)), "✉️ Email"))))'
    )


@dataclass
class LogEntry:
    row: int
    article: Article
    status: str
    attempts: int


def open_spreadsheet() -> gspread.Spreadsheet:
    """GOOGLE_SERVICE_ACCOUNT_JSON may hold the key JSON itself (GitHub Actions) or a path to it (local)."""
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"].strip()
    if not raw.startswith("{"):
        path = Path(raw)
        if not path.is_absolute():
            # Relative paths in .env are relative to tracker/, wherever the script is started from.
            path = Path(__file__).parent / path
        if not path.exists():
            raise SystemExit(
                f"GOOGLE_SERVICE_ACCOUNT_JSON is '{raw}', but no such key file exists. "
                "Locally, point it at the downloaded key file. In GitHub Secrets, paste the file's "
                "entire JSON content (starting with '{'), not its name."
            )
        raw = path.read_text(encoding="utf-8")
    info = json.loads(raw)
    return gspread.service_account_from_dict(info).open_by_key(os.environ["SHEET_ID"])


def add_email_buttons(ws: gspread.Worksheet, first_row: int, last_row: int) -> None:
    if last_row >= first_row:
        ws.update([[email_formula()]] * (last_row - first_row + 1),
                  f"{EMAIL_COL}{first_row}:{EMAIL_COL}{last_row}", value_input_option="USER_ENTERED")


def _first_row(append_response: dict) -> int:
    # e.g. "'Article Log'!A12:H14" -> 12
    rng = append_response["updates"]["updatedRange"]
    return int(re.search(r"![A-Z]+(\d+)", rng).group(1))


class SheetStore:
    def __init__(self, spreadsheet: gspread.Spreadsheet):
        self._raw = spreadsheet.worksheet(RAW)
        self._log = spreadsheet.worksheet(ARTICLE_LOG)

    def existing_records(self) -> list[EventRecord]:
        """Every round already in Raw Database, read by header name, with its sheet row."""
        values = self._raw.get_all_values()
        if not values:
            return []
        header = values[0]
        records = []
        for i, r in enumerate(values[1:], start=2):
            d = dict(zip(header, r))
            if not d.get("Company"):
                continue
            records.append(EventRecord(
                event_key(d["Company"], d.get("Funding Round", ""), d.get("Date", "")),
                {h: d.get(h, "") for h in DATA_HEADERS},
                row=i,
            ))
        return records

    def delete_rows(self, rows: list[int]) -> None:
        """Delete Raw Database rows, bottom first so the other row numbers stay valid."""
        for row in sorted(set(rows), reverse=True):
            self._raw.delete_rows(row)

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

    def append_events(self, rows: list[dict[str, str]]) -> None:
        """rows: {header: value} for every Raw Database column except Email."""
        if not rows:
            return
        # Data goes in RAW so nothing is reinterpreted (dates stay text, "=..." names stay text) ...
        resp = self._raw.append_rows([[r.get(h, "") for h in RAW_HEADERS] for r in rows],
                                     value_input_option="RAW", table_range="A1")
        # ... and only the Email buttons are written as formulas.
        start = _first_row(resp)
        add_email_buttons(self._raw, start, start + len(rows) - 1)

    def update_statuses(self, updates: list[tuple[int, str, int, str]]) -> None:
        """updates: (row, status, attempts, error)."""
        if updates:
            self._log.batch_update(
                [{"range": f"F{row}:H{row}", "values": [[status, attempts, error[:500]]]}
                 for row, status, attempts, error in updates],
                value_input_option="RAW",
            )
