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
from normalize import (
    EventRecord, event_key, find_match, merge_update, normalize_amount, normalize_round, parse_date,
)
from parser import article_text

HERE = Path(__file__).parent
log = logging.getLogger("tracker")


def event_rows(article: Article, extraction, existing: list[EventRecord], window: int, today: str):
    """Fold the extracted events into `existing` (every round in the sheet plus this run's changes).

    A new round becomes a new record. A later report of a round already there replaces it when it adds
    details (e.g. the round was "Undisclosed" and is now "Seed"); otherwise it is skipped.
    Returns (added, updated, skipped): the records added and updated, and the number skipped.
    """
    added, updated, skipped = [], [], 0
    for ev in extraction.events:
        if not ev.company.strip():
            continue
        round_ = normalize_round(ev.round)
        when = parse_date(ev.date) or (article.published.date() if article.published else None)
        values = {
            "Date": when.isoformat() if when else "",
            "Company": ev.company.strip(),
            "Funding Round": round_,
            "Amount Raised": normalize_amount(ev.amount),
            "Valuation": normalize_amount(ev.valuation) if ev.valuation.strip() else "",
            "Lead Investor": ev.lead_investor.strip(),
            "Other Investors": ", ".join(i.strip() for i in ev.other_investors if i.strip()),
            "Category": ev.category.strip(),
            "Source": article.source,
            "Article URL": article.url,
            "Date Added": today,
        }
        key = event_key(ev.company, round_, when)
        i = find_match(key, [e.key for e in existing], window)
        if i is None:
            rec = EventRecord(key, values)
            existing.append(rec)
            added.append(rec)
            continue
        old = existing[i]
        merged = merge_update(old.values, values)
        if merged is None:
            skipped += 1
            continue
        # The sheet row (if any) is deleted and the merged round appended as today's row.
        rec = EventRecord(event_key(merged["Company"], merged["Funding Round"], merged["Date"]), merged,
                          replaces=old.replaces + ([old.row] if old.row else []))
        existing[i] = rec
        same_run = [j for j, a in enumerate(added) if a is old]
        if same_run:
            added[same_run[0]] = rec  # added earlier in this run: still just one new row
        else:
            updated.append(rec)
    return added, updated, skipped


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
        existing = store.existing_records()
        log.info("New articles: %d, retries: %d, events already in sheet: %d", len(fresh), len(retry), len(existing))

    statuses = []
    counts = {"funding": 0, "not_funding": 0, "added": 0, "updated": 0, "duplicates": 0, "errors": 0, "failed": 0}
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

        added, updated, skipped = event_rows(article, extraction, existing, window, today)
        counts["funding"] += 1
        counts["added"] += len(added)
        counts["updated"] += len(updated)
        counts["duplicates"] += skipped
        statuses.append((row, "extracted", attempts + 1, ""))
        for mark, recs in (("+", added), ("~", updated)):
            for r in recs:
                v = r.values
                log.info("%s %s | %s | %s | %s | %s | %s", mark, v["Date"], v["Company"], v["Funding Round"],
                         v["Amount Raised"], v["Valuation"], v["Lead Investor"])

    if store:
        # Rows first: if this fails, statuses stay "pending" and the next run retries (dedup prevents doubles).
        # New and updated rounds are appended before the rows they replace are deleted, so nothing is lost.
        pending = [e for e in existing if e.row is None]
        store.append_events([e.values for e in pending])
        store.delete_rows([r for e in pending for r in e.replaces])
        store.update_statuses(statuses)

    log.info(
        "Done. processed=%d funding_articles=%d added=%d updated=%d duplicates=%d not_funding=%d errors=%d failed=%d",
        len(work), counts["funding"], counts["added"], counts["updated"], counts["duplicates"],
        counts["not_funding"], counts["errors"], counts["failed"],
    )
    # Fail the job (GitHub emails you) only when something needs a human: every feed down, an article
    # that has used up its retries, or a Gemini quota too small for the day's news. Transient errors are
    # retried on the next run.
    return 1 if counts["failed"] or quota_error or len(failed_sources) == len(enabled) else 0


if __name__ == "__main__":
    sys.exit(main())
