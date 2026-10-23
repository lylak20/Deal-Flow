"""Daily run: collect -> parse -> extract -> append to Google Sheets.

    python main.py             # full run
    python main.py --dry-run   # only list candidate articles (no API keys needed)
    python main.py --no-write  # extract with Gemini and print rows, but don't touch the sheet
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import urllib3.util.connection
import yaml
from dotenv import load_dotenv

from collector import Article, collect
from extractor import QuotaExhausted, make_extractor
from normalize import EventKey, event_key, is_duplicate, normalize_amount, normalize_round, parse_date
from parser import article_text

HERE = Path(__file__).parent
log = logging.getLogger("tracker")


def event_rows(article: Article, extraction, existing: list[EventKey], window: int, today: str):
    """Turn Gemini output into Raw Database rows, skipping duplicates. Adds accepted keys to `existing`."""
    rows, skipped = [], 0
    for ev in extraction.events:
        if not ev.company.strip():
            continue
        round_ = normalize_round(ev.round)
        when = parse_date(ev.date) or (article.published.date() if article.published else None)
        when_str = when.isoformat() if when else ""
        key = event_key(ev.company, round_, when)
        if is_duplicate(key, existing, window):
            skipped += 1
            continue
        existing.append(key)
        rows.append([
            when_str, ev.company.strip(), round_, normalize_amount(ev.amount), ev.lead_investor.strip(),
            ", ".join(i.strip() for i in ev.other_investors if i.strip()), ev.category.strip(),
            article.source, article.url, today,
        ])
    return rows, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list candidate articles only")
    ap.add_argument("--no-write", action="store_true", help="extract but do not write to the sheet")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(HERE / ".env")
    config = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    if config["http"].get("ipv4_only"):
        urllib3.util.connection.HAS_IPV6 = False
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    candidates, failed_sources = collect(config, now)
    enabled = [s for s in config["sources"] if s.get("enabled", True)]
    log.info("Candidates: %d (failed sources: %d/%d)", len(candidates), len(failed_sources), len(enabled))

    if args.dry_run:
        for a in sorted(candidates, key=lambda a: a.published or now, reverse=True):
            print(f"{a.published:%Y-%m-%d %H:%M}  [{a.source}]  {a.title}\n    {a.url}" if a.published
                  else f"(no date)  [{a.source}]  {a.title}\n    {a.url}")
        return 1 if len(failed_sources) == len(enabled) else 0

    extractor = make_extractor(config, os.environ)
    max_attempts = config["max_attempts_per_article"]
    window = config["dedup_window_days"]

    if args.no_write:
        store, existing = None, []
        work = {a.url: (None, a, 0) for a in candidates}
    else:
        from sheets import SheetStore, open_spreadsheet
        store = SheetStore(open_spreadsheet())
        logged = store.article_log()
        new = [a for a in candidates if a.url not in logged]
        fresh = store.log_new_articles(new, now.isoformat(timespec="seconds"))
        retry = {u: e for u, e in logged.items() if e.status in ("pending", "error") and e.attempts < max_attempts}
        work = {u: (e.row, e.article, e.attempts) for u, e in {**retry, **fresh}.items()}
        existing = store.existing_events()
        log.info("New articles: %d, retries: %d, events already in sheet: %d", len(fresh), len(retry), len(existing))

    all_rows, statuses = [], []
    counts = {"funding": 0, "not_funding": 0, "duplicates": 0, "errors": 0, "failed": 0}
    quota_error = None
    for row, article, attempts in work.values():
        try:
            extraction = extractor.extract(article, article_text(article, config))
        except QuotaExhausted as e:
            # Not the article's fault: leave it and the rest "pending" (attempts unchanged) for the next run.
            quota_error = e
            log.error("%s. Stopping; unprocessed articles stay pending.", e)
            break
        except Exception as e:
            attempts += 1
            status = "failed" if attempts >= max_attempts else "error"
            counts["failed" if status == "failed" else "errors"] += 1
            log.error("Extraction %s for %s: %s", status, article.url, e)
            statuses.append((row, status, attempts, str(e)))
            continue

        if not extraction.is_funding_announcement or not extraction.events:
            counts["not_funding"] += 1
            statuses.append((row, "skipped_not_funding", attempts + 1, ""))
            continue

        rows, skipped = event_rows(article, extraction, existing, window, today)
        counts["funding"] += 1
        counts["duplicates"] += skipped
        all_rows.extend(rows)
        statuses.append((row, "extracted", attempts + 1, ""))
        for r in rows:
            log.info("+ %s | %s | %s | %s | %s", r[0], r[1], r[2], r[3], r[4])

    if store:
        # Events first: if this fails, statuses stay "pending" and the next run retries (dedup prevents doubles).
        store.append_events(all_rows)
        store.update_statuses(statuses)

    log.info(
        "Done. processed=%d funding_articles=%d new_rows=%d duplicates=%d not_funding=%d errors=%d failed=%d",
        len(work), counts["funding"], len(all_rows), counts["duplicates"], counts["not_funding"],
        counts["errors"], counts["failed"],
    )
    # Fail the job (GitHub emails you) only when something needs a human: every feed down, an article
    # that has used up its retries, or a Gemini quota too small for the day's news. Transient errors are
    # retried on the next run.
    return 1 if counts["failed"] or quota_error or len(failed_sources) == len(enabled) else 0


if __name__ == "__main__":
    sys.exit(main())
