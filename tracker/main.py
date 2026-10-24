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

from collector import Article, collect, guess_company, is_headline_only
from extractor import QuotaExhausted, make_extractor
from normalize import (
    EventRecord, company_key, event_key, find_match, merge_update, normalize_amount, normalize_round, parse_date,
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


STALE_HEADLINE_DAYS = 3
KNOWN_COMPANY_DAYS = 7


TRACKS = ("all", "robotics")


def _sources_named(config: dict, source: str) -> list[dict]:
    if source.endswith("(via Google News)"):
        source = "Google News"
    return [s for s in config["sources"] if s["name"] == source]


def source_priority(config: dict, source: str) -> int:
    return min((s.get("priority", 2) for s in _sources_named(config, source)), default=2)


def source_tracks(config: dict, source: str) -> tuple[str, ...]:
    """Tracks an article from this source can feed (for retries rebuilt from the Article Log)."""
    tracks = {t for s in _sources_named(config, source) for t in s.get("tracks", TRACKS)}
    return tuple(t for t in TRACKS if t in tracks) or TRACKS


def run_extraction(work: list[tuple], extractor, existing: dict[str, list[EventRecord]], config: dict,
                   now: datetime):
    """Extract events from (log_row, article, attempts) items, highest-priority sources first.

    One LLM call per article feeds both tracks: every round goes to existing["all"] when the article's
    source feeds that track, and in-focus rounds go to existing["robotics"].

    LLM calls are saved by skipping, without a call: headline-only items that are stale or about a company
    added in the last few days, and any article about a company another article already covered this run.
    Stops at max_llm_calls_per_run or when every provider is out of quota; what's left stays "pending".
    Returns (Article Log status updates, counts, the QuotaExhausted error if the run was cut short).
    """
    today = now.date()
    window = config["dedup_window_days"]
    max_attempts = config["max_attempts_per_article"]
    budget = config.get("max_llm_calls_per_run", 10**9)
    statuses = []
    counts = dict.fromkeys(["llm_calls", "funding", "all_added", "all_updated", "robotics_added", "robotics_updated",
                            "duplicates", "not_funding", "not_in_focus", "same_story", "known_company", "stale",
                            "errors", "failed", "left_pending"], 0)
    covered: set[str] = set()  # company keys extracted this run
    quota_error = None

    def recently_added(company: str) -> bool:
        for e in existing["robotics"]:
            d = parse_date(e.values.get("Date"))
            if e.key.company == company and d and (today - d).days <= KNOWN_COMPANY_DAYS:
                return True
        return False

    order = sorted(work, key=lambda w: (w[1].priority, -(w[1].published.timestamp() if w[1].published else 0)))
    for n, (row, article, attempts) in enumerate(order):
        guess = guess_company(article.title)
        headline = is_headline_only(article)
        if headline and article.published and (now - article.published).days > STALE_HEADLINE_DAYS:
            counts["stale"] += 1
            statuses.append((row, "skipped_stale", attempts, ""))
            continue
        if guess and guess in covered:
            counts["same_story"] += 1
            statuses.append((row, "skipped_same_story", attempts, ""))
            continue
        if headline and guess and recently_added(guess):
            counts["known_company"] += 1
            statuses.append((row, "skipped_known_company", attempts, ""))
            continue
        if counts["llm_calls"] >= budget:
            counts["left_pending"] = len(order) - n
            log.warning("Reached max_llm_calls_per_run (%d); %d articles stay pending", budget, len(order) - n)
            break

        counts["llm_calls"] += 1
        try:
            text = article_text(article, config)
            extraction = extractor.extract(article, text)
        except QuotaExhausted as e:
            # Not the article's fault: leave it and the rest "pending" (attempts unchanged) for the next run.
            quota_error = e
            counts["left_pending"] = len(order) - n
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
        covered.update(company_key(ev.company) for ev in extraction.events)
        if guess:
            covered.add(guess)
        events = extraction.events
        routed = {
            "all": events if "all" in article.tracks else [],
            "robotics": [ev for ev in events if ev.in_focus],
        }
        if not routed["all"] and not routed["robotics"]:
            counts["not_in_focus"] += 1
            statuses.append((row, "skipped_not_in_focus", attempts + 1, ""))
            continue

        counts["funding"] += 1
        statuses.append((row, "extracted", attempts + 1, ""))
        for track, evs in routed.items():
            if not evs:
                continue
            added, updated, skipped = event_rows(
                article, extraction.model_copy(update={"events": evs}), existing[track], window, today.isoformat())
            counts[f"{track}_added"] += len(added)
            counts[f"{track}_updated"] += len(updated)
            counts["duplicates"] += skipped
            for mark, recs in (("+", added), ("~", updated)):
                for r in recs:
                    v = r.values
                    log.info("%s [%s] %s | %s | %s | %s | %s", mark, track, v["Date"], v["Company"],
                             v["Funding Round"], v["Amount Raised"], v["Category"])
    return statuses, counts, quota_error


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

    candidates, failed_sources = collect(config, now)
    enabled = [s for s in config["sources"] if s.get("enabled", True)]
    log.info("Candidates: %d (failed sources: %d/%d)", len(candidates), len(failed_sources), len(enabled))

    if args.dry_run:
        for a in sorted(candidates, key=lambda a: (a.priority, -(a.published or now).timestamp())):
            when = f"{a.published:%Y-%m-%d %H:%M}" if a.published else "(no date)"
            print(f"P{a.priority} {when}  [{a.source}]  {a.title}\n    company={guess_company(a.title)}  {a.url[:100]}")
        return 1 if len(failed_sources) == len(enabled) else 0

    extractor = make_extractor(config, os.environ)
    max_attempts = config["max_attempts_per_article"]

    if args.no_write:
        store, existing = None, {t: [] for t in TRACKS}
        work = {a.url: (None, a, 0) for a in candidates}
    else:
        from sheets import SheetStore, open_spreadsheet
        store = SheetStore(open_spreadsheet())
        logged = store.article_log()
        new = [a for a in candidates if a.url not in logged]
        fresh = store.log_new_articles(new, now.isoformat(timespec="seconds"))
        retry = {u: e for u, e in logged.items() if e.status in ("pending", "error") and e.attempts < max_attempts}
        for e in retry.values():
            # Rebuilt from the Article Log, so it lacks its feed's priority and tracks.
            e.article.priority = source_priority(config, e.article.source)
            e.article.tracks = source_tracks(config, e.article.source)
        work = {u: (e.row, e.article, e.attempts) for u, e in {**retry, **fresh}.items()}
        existing = {t: store.existing_records(t) for t in TRACKS}
        log.info("New articles: %d, retries: %d, rows already in sheet: %s", len(fresh), len(retry),
                 {t: len(v) for t, v in existing.items()})

    statuses, counts, quota_error = run_extraction(list(work.values()), extractor, existing, config, now)

    if store:
        # Rows first: if this fails, statuses stay "pending" and the next run retries (dedup prevents doubles).
        # New and updated rounds are appended before the rows they replace are deleted, so nothing is lost.
        for track, records in existing.items():
            pending = [e for e in records if e.row is None]
            store.append_events(track, [e.values for e in pending])
            store.delete_rows(track, [r for e in pending for r in e.replaces])
        store.update_statuses(statuses)

    log.info("Done. articles=%d %s", len(work), " ".join(f"{k}={v}" for k, v in counts.items()))
    # Fail the job (GitHub emails you) only when something needs a human: every feed down, an article
    # that has used up its retries, or a Gemini quota too small for the day's news. Transient errors are
    # retried on the next run.
    return 1 if counts["failed"] or quota_error or len(failed_sources) == len(enabled) else 0


if __name__ == "__main__":
    sys.exit(main())
