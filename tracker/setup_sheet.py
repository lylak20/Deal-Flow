"""Set up (or upgrade) the Google Sheet: tabs, headers, dashboard formulas, Email buttons, links and template.

Safe to run again: data rows are kept, a user-edited email template is kept, a database tab created
before a column existed (Email, Valuation) gets that column inserted in place, and retired columns
(Source, Website) are deleted.

    python setup_sheet.py
"""

import gspread
from dotenv import load_dotenv

from sheets import (
    ARTICLE_LOG, DASHBOARD_ROUNDS, DASHBOARDS, DEFAULT_BODY, DEFAULT_SUBJECT, EMAIL_TEMPLATE, LOG_HEADERS,
    RAW_HEADERS, RETIRED_HEADERS, TABS, add_email_buttons, col, open_spreadsheet,
)

# Tab colors: the funding databases and their dashboards, and the Sourcing tabs.
YELLOW = {"red": 1.0, "green": 0.949, "blue": 0.8}     # #FFF2CC
GREEN = {"red": 0.851, "green": 0.918, "blue": 0.827}  # #D9EAD3


def dashboard_formula(round_name: str, tab: str) -> str:
    # "Company (YYYY-MM-DD)" for every row of this round, newest first. Updates itself as rows are appended.
    r = f"'{tab}'"
    date, company, round_ = (f"{r}!{col(h)}2:{col(h)}" for h in ("Date", "Company", "Funding Round"))
    return (
        f'=IFERROR(CHOOSECOLS(SORT(FILTER({{{company} & " (" & {date} & ")", {date}}}, '
        f'{round_} = "{round_name}"), 2, FALSE), 1), "")'
    )


def ensure_tab(spreadsheet: gspread.Spreadsheet, title: str, cols: int) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=cols)


def setup_database(ss: gspread.Spreadsheet, tab: str) -> None:
    ws = ensure_tab(ss, tab, len(RAW_HEADERS))
    header = ws.row_values(1)
    retired = [i for i, h in enumerate(header) if h in RETIRED_HEADERS]
    if retired:
        # Rightmost first, so the other column numbers stay valid.
        for i in reversed(retired):
            ws.delete_columns(i + 1)
            print(f"{tab}: deleted the {header[i]} column")
        header = [h for h in header if h not in RETIRED_HEADERS]
        # Company names used to link to the company website (cell formatting); drop those links.
        c = RAW_HEADERS.index("Company")
        ws.spreadsheet.batch_update({"requests": [{"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "startColumnIndex": c, "endColumnIndex": c + 1},
            "cell": {"userEnteredFormat": {"textFormat": {}}},
            "fields": "userEnteredFormat.textFormat.link",
        }}]})
    if not header:
        ws.update([RAW_HEADERS], "A1", value_input_option="RAW")
    elif header != RAW_HEADERS:
        # A tab from before a column was added (Email, Valuation): insert each missing column where
        # it belongs, which keeps every data row aligned. Anything else needs a human.
        if [h for h in RAW_HEADERS if h in header] != header:
            raise SystemExit(f"Unexpected {tab} header {header}; fix it by hand before running setup.")
        for i, h in enumerate(RAW_HEADERS):
            if h not in header:
                if i < len(header):
                    ws.insert_cols([[h]], col=i + 1, value_input_option="RAW")
                else:
                    # Past the last column: the grid may have to grow before the header can be written.
                    if i >= ws.col_count:
                        ws.add_cols(i + 1 - ws.col_count)
                    ws.update([[h]], f"{col(h)}1", value_input_option="RAW")
                header.insert(i, h)
                print(f"{tab}: inserted the {h} column")
    ws.freeze(rows=1)
    add_email_buttons(ws, 2, len(ws.get_all_values()))


def order_tabs(ss: gspread.Spreadsheet) -> None:
    """Put each database tab followed by its dashboard first; every other tab keeps its relative order."""
    first = [t for db in TABS.values() for t in (db, *(d for d, src in DASHBOARDS.items() if src == db))]
    tabs = ss.worksheets()
    by_title = {ws.title: ws for ws in tabs}
    wanted = [by_title[t] for t in first if t in by_title] + [ws for ws in tabs if ws.title not in first]
    if [ws.title for ws in wanted] != [ws.title for ws in tabs]:
        ss.reorder_worksheets(wanted)


def style_tabs(ss: gspread.Spreadsheet) -> None:
    """Hide the Article Log (the program's own bookkeeping) and color the tabs.

    Yellow: each database and its dashboard. Green: every Sourcing tab. Email Template stays uncolored.
    """
    funding = {*TABS.values(), *DASHBOARDS}
    requests = []
    for ws in ss.worksheets():
        props = {"sheetId": ws.id}
        if ws.title == ARTICLE_LOG:
            props["hidden"] = True
            requests.append({"updateSheetProperties": {"properties": props, "fields": "hidden"}})
        elif ws.title != EMAIL_TEMPLATE:
            props["tabColorStyle"] = {"rgbColor": YELLOW if ws.title in funding else GREEN}
            requests.append({"updateSheetProperties": {"properties": props, "fields": "tabColorStyle"}})
    if requests:
        ss.batch_update({"requests": requests})


def setup_template(ss: gspread.Spreadsheet) -> None:
    tmpl = ensure_tab(ss, EMAIL_TEMPLATE, 2)
    body = tmpl.acell("B2").value or ""
    # Empty, or still the first version's generic text ("[Your name]"): write the current default.
    if not body or "[Your name]" in body:
        tmpl.update([
            ["Subject", DEFAULT_SUBJECT],
            ["Body", DEFAULT_BODY],
            [],
            ["Placeholders", "{Company}, {Round} (\"recent round\" if unknown), "
                             "{Amount} (\" ($66M)\" or nothing), {Lead} (\"your investors\" if unknown)"],
            ["How it works", "Click \"✉️ Email\" in Raw Database or Robotics Database: Gmail opens a draft filled "
                             "from this template. Edit B1/B2 to change every button at once. Gmail draft links "
                             "can't carry formatting, so links stay plain URLs (Gmail makes them clickable) and "
                             "[Name] is yours to fill in."],
        ], "A1", value_input_option="RAW")
        tmpl.format("B2", {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"})
        tmpl.format("A1:A5", {"textFormat": {"bold": True}, "verticalAlignment": "TOP"})


def main() -> None:
    load_dotenv()
    ss = open_spreadsheet()

    # The template must exist before the Email formulas that reference it.
    setup_template(ss)
    for tab in TABS.values():
        setup_database(ss, tab)

    for title, tab in DASHBOARDS.items():
        dash = ensure_tab(ss, title, len(DASHBOARD_ROUNDS))
        dash.update(
            [DASHBOARD_ROUNDS, [dashboard_formula(r, tab) for r in DASHBOARD_ROUNDS]],
            "A1",
            value_input_option="USER_ENTERED",
        )
        dash.freeze(rows=1)

    log = ensure_tab(ss, ARTICLE_LOG, len(LOG_HEADERS))
    if log.row_values(1) != LOG_HEADERS:
        log.update([LOG_HEADERS], "A1", value_input_option="RAW")
    log.freeze(rows=1)

    order_tabs(ss)
    style_tabs(ss)

    # A new spreadsheet starts with an empty default tab; remove it once our tabs exist.
    for ws in ss.worksheets():
        if ws.title in ("Sheet1", "시트1") and not any(ws.get_all_values()):
            ss.del_worksheet(ws)

    print(f"Sheet ready: {ss.url}")


if __name__ == "__main__":
    main()
