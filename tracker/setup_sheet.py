"""One-time setup: create the tabs, headers and dashboard formulas in an existing, empty Google Sheet.

Safe to run again: existing tabs are left alone (only missing headers/formulas are written).

    python setup_sheet.py
"""

import gspread
from dotenv import load_dotenv

from sheets import ARTICLE_LOG, DASHBOARD, DASHBOARD_ROUNDS, LOG_HEADERS, RAW, RAW_HEADERS, open_spreadsheet


def dashboard_formula(round_name: str) -> str:
    # "Company (YYYY-MM-DD)" for every row of this round, newest first. Updates itself as rows are appended.
    r = f"'{RAW}'"
    return (
        f'=IFERROR(CHOOSECOLS(SORT(FILTER({{{r}!B2:B & " (" & {r}!A2:A & ")", {r}!A2:A}}, '
        f'{r}!C2:C = "{round_name}"), 2, FALSE), 1), "")'
    )


def ensure_tab(spreadsheet: gspread.Spreadsheet, title: str, cols: int) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=cols)


def main() -> None:
    load_dotenv()
    ss = open_spreadsheet()

    raw = ensure_tab(ss, RAW, len(RAW_HEADERS))
    if raw.row_values(1) != RAW_HEADERS:
        raw.update([RAW_HEADERS], "A1", value_input_option="RAW")
    raw.freeze(rows=1)

    dash = ensure_tab(ss, DASHBOARD, len(DASHBOARD_ROUNDS))
    dash.update(
        [DASHBOARD_ROUNDS, [dashboard_formula(r) for r in DASHBOARD_ROUNDS]],
        "A1",
        value_input_option="USER_ENTERED",
    )
    dash.freeze(rows=1)

    log = ensure_tab(ss, ARTICLE_LOG, len(LOG_HEADERS))
    if log.row_values(1) != LOG_HEADERS:
        log.update([LOG_HEADERS], "A1", value_input_option="RAW")
    log.freeze(rows=1)

    # A new spreadsheet starts with an empty "Sheet1"; remove it once our tabs exist.
    for ws in ss.worksheets():
        if ws.title not in (RAW, DASHBOARD, ARTICLE_LOG) and not any(ws.get_all_values()):
            ss.del_worksheet(ws)

    print(f"Sheet ready: {ss.url}")


if __name__ == "__main__":
    main()
