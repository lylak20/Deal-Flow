"""Set up (or upgrade) the Google Sheet: tabs, headers, dashboard formulas, Email buttons, links and template.

Safe to run again: data rows are kept, a user-edited email template is kept, and a database tab created
before a column existed (Email, Valuation, Website) gets that column inserted in place.

    python setup_sheet.py
"""

import gspread
from dotenv import load_dotenv

from sheets import (
    ARTICLE_LOG, DASHBOARD_ROUNDS, DASHBOARDS, DATA_HEADERS, DEFAULT_BODY, DEFAULT_SUBJECT, EMAIL_TEMPLATE,
    LOG_HEADERS, RAW_HEADERS, TABS, add_company_links, add_email_buttons, col, open_spreadsheet,
)


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
    if not header:
        ws.update([RAW_HEADERS], "A1", value_input_option="RAW")
    elif header != RAW_HEADERS:
        # A tab from before a column was added (Email, Valuation, Website): insert each missing column where
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
    values = ws.get_all_values()
    add_email_buttons(ws, 2, len(values))
    add_company_links(ws, [(i, {h: d for h, d in zip(RAW_HEADERS, r) if h in DATA_HEADERS})
                           for i, r in enumerate(values[1:], start=2)])


def setup_template(ss: gspread.Spreadsheet) -> None:
    tmpl = ensure_tab(ss, EMAIL_TEMPLATE, 2)
    if not any(tmpl.col_values(2)):
        tmpl.update([
            ["Subject", DEFAULT_SUBJECT],
            ["Body", DEFAULT_BODY],
            [],
            ["Placeholders", "{Company}, {Round} (\"recent round\" if unknown), "
                             "{Amount} (\" ($66M)\" or nothing), {Lead} (\"your investors\" if unknown)"],
            ["How it works", "Click \"✉️ Email\" in Raw Database or Robotics Database: Gmail opens a draft filled "
                             "from this template. Edit B1/B2 to change every button at once."],
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

    # A new spreadsheet starts with an empty default tab; remove it once our tabs exist.
    for ws in ss.worksheets():
        if ws.title in ("Sheet1", "시트1") and not any(ws.get_all_values()):
            ss.del_worksheet(ws)

    print(f"Sheet ready: {ss.url}")


if __name__ == "__main__":
    main()
