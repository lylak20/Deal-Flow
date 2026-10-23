"""Set up (or upgrade) the Google Sheet: tabs, headers, dashboard formulas, Email buttons and template.

Safe to run again: data rows are kept, a user-edited email template is kept, and a sheet created
before the Email column existed gets that column inserted left of Company.

    python setup_sheet.py
"""

import gspread
from dotenv import load_dotenv

from sheets import (
    ARTICLE_LOG, DASHBOARD, DASHBOARD_ROUNDS, DEFAULT_BODY, DEFAULT_SUBJECT, EMAIL_TEMPLATE, LOG_HEADERS, RAW,
    RAW_HEADERS, add_email_buttons, col, open_spreadsheet,
)


def dashboard_formula(round_name: str) -> str:
    # "Company (YYYY-MM-DD)" for every row of this round, newest first. Updates itself as rows are appended.
    r = f"'{RAW}'"
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


def setup_raw(ss: gspread.Spreadsheet) -> None:
    raw = ensure_tab(ss, RAW, len(RAW_HEADERS))
    header = raw.row_values(1)
    without_email = [h for h in RAW_HEADERS if h != "Email"]
    if header == without_email:
        # Sheet from before the Email column: insert it left of Company, keeping every data row aligned.
        raw.insert_cols([["Email"]], col=RAW_HEADERS.index("Email") + 1, value_input_option="RAW")
        print("Inserted the Email column left of Company")
    elif header != RAW_HEADERS:
        if header:
            raise SystemExit(f"Unexpected Raw Database header {header}; fix it by hand before running setup.")
        raw.update([RAW_HEADERS], "A1", value_input_option="RAW")
    raw.freeze(rows=1)
    last = len(raw.col_values(RAW_HEADERS.index("Company") + 1))
    add_email_buttons(raw, 2, last)


def setup_template(ss: gspread.Spreadsheet) -> None:
    tmpl = ensure_tab(ss, EMAIL_TEMPLATE, 2)
    if not any(tmpl.col_values(2)):
        tmpl.update([
            ["Subject", DEFAULT_SUBJECT],
            ["Body", DEFAULT_BODY],
            [],
            ["Placeholders", "{Company}, {Round} (\"recent round\" if unknown), "
                             "{Amount} (\" ($66M)\" or nothing), {Lead} (\"your investors\" if unknown)"],
            ["How it works", "Click \"✉️ Email\" in Raw Database: Gmail opens a draft filled from this template. "
                             "Edit B1/B2 to change every button at once."],
        ], "A1", value_input_option="RAW")
        tmpl.format("B2", {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"})
        tmpl.format("A1:A5", {"textFormat": {"bold": True}, "verticalAlignment": "TOP"})


def main() -> None:
    load_dotenv()
    ss = open_spreadsheet()

    # The template must exist before the Email formulas that reference it.
    setup_template(ss)
    setup_raw(ss)

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
        if ws.title not in (RAW, DASHBOARD, ARTICLE_LOG, EMAIL_TEMPLATE) and not any(ws.get_all_values()):
            ss.del_worksheet(ws)

    print(f"Sheet ready: {ss.url}")


if __name__ == "__main__":
    main()
