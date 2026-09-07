"""Entry point for the Inspire / WomenHeart corpus pipeline.

    python src/pipeline.py run       crawl new threads -> clean (default; clean is
                                     skipped when nothing new was found)
    python src/pipeline.py crawl     crawl only
    python src/pipeline.py clean     clean only
    python src/pipeline.py status    show the current state of the corpus

Incremental semantics
---------------------
"Incremental" here means *discover new threads only*: threads that have already been
crawled are never revisited, so **replies posted later on an old thread are not picked
up**. This is a deliberate trade-off -- in practice this community produced new activity
on only 3 threads over 30 days and averages ~2 new threads a month, which does not
justify maintaining a `lastmod` comparison just to chase incremental replies. To rebuild
the corpus from scratch, delete the .jsonl checkpoint.

Data flow
---------
    sitemap --> womenheart_v3.jsonl        append-one-line-per-thread checkpoint;
                      |                    the resume anchor and the source of truth
                      |
                      +--> womenheart_v3_posts.csv     raw (not de-identified)
                      |    womenheart_v3_replies.csv
                      |
                      +--> womenheart_clean_posts.csv   cleaned (de-identified)
                           womenheart_clean_replies.csv
                           womenheart_clean_links.csv   extracted full URLs
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# This file lives in src/, so the project root is one level up.
ROOT = Path(__file__).resolve().parent.parent
GROUP = "womenheart"

# Every path is anchored to the project root, so the pipeline runs correctly from any
# working directory (which matters most when it is driven by a scheduler).
DATA = ROOT / "data"
RAW = DATA / "raw" / "womenheart_v3"         # raw output prefix (no extension)
CLEAN = DATA / "clean" / "womenheart_clean"  # cleaned output prefix
REPORT = DATA / "clean" / "clean_report.txt"
LOGDIR = ROOT / "logs"
LOCK = ROOT / ".pipeline.lock"

SITEMAP_TOTAL_HINT = 12488            # only used to show a progress percentage in status

log = logging.getLogger("pipeline")


# ---------------- Infrastructure ----------------

def ensure_dirs():
    for d in (DATA / "raw", DATA / "clean", LOGDIR):
        d.mkdir(parents=True, exist_ok=True)


def setup_logging(verbose=True):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOGDIR / f"pipeline_{datetime.now():%Y%m%d}.log",
                             encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


class RunLock:
    """Stop two instances from appending to the same .jsonl and interleaving bad lines."""

    def __enter__(self):
        if LOCK.exists():
            try:
                info = json.loads(LOCK.read_text(encoding="utf-8"))
            except Exception:
                info = {}
            pid = info.get("pid")
            if pid and _pid_alive(pid):
                raise SystemExit(
                    f"Another instance is already running (PID {pid}, "
                    f"started {info.get('started')}).\n"
                    f"If you are sure it is gone, delete {LOCK.name} and retry.")
            log.warning("Found a stale lock (PID %s no longer exists), taking it over", pid)
        LOCK.write_text(json.dumps(
            {"pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds")}),
            encoding="utf-8")
        return self

    def __exit__(self, *exc):
        LOCK.unlink(missing_ok=True)
        return False


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def checkpoint_stats():
    """Read the current corpus state from the .jsonl checkpoint (fast, ignores the CSVs)."""
    p = Path(f"{RAW}.jsonl")
    if not p.exists():
        return None
    posts = replies = 0
    newest_post = newest_reply = ""
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            posts += 1
            replies += len(rec.get("replies", []))
            t = rec["post"].get("publish_time_iso") or ""
            if t > newest_post:
                newest_post = t
            for r in rec.get("replies", []):
                t = r.get("publish_time_iso") or ""
                if t > newest_reply:
                    newest_reply = t
    return dict(posts=posts, replies=replies,
                newest_post=newest_post, newest_reply=newest_reply,
                size_mb=round(p.stat().st_size / 1e6, 1),
                mtime=datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"))


# ---------------- Steps ----------------

def step_crawl(target, relevance):
    from scraper_v3 import WomenHeartCrawler

    before = checkpoint_stats() or {"posts": 0, "replies": 0}
    log.info("Crawl started | have %s posts / %s replies | target %s",
             f"{before['posts']:,}", f"{before['replies']:,}",
             f"{target:,}" if target else "whole sitemap")

    c = WomenHeartCrawler(group=GROUP, target_count=target, out=str(RAW),
                          relevance=relevance, resume=True)
    t0 = time.time()
    c.run()
    c.save()

    after = checkpoint_stats()
    new_p = after["posts"] - before["posts"]
    new_r = after["replies"] - before["replies"]
    log.info("Crawl finished | added %s posts / %s replies | took %.1f min",
             f"{new_p:,}", f"{new_r:,}", (time.time() - t0) / 60)
    if c.failed:
        log.warning("Failed to crawl %d URLs (listed below)", len(c.failed))
        for u in c.failed[:10]:
            log.warning("    %s", u)
    return new_p, new_r


def step_clean():
    import clean_v3

    t0 = time.time()
    log.info("Clean started")
    res = clean_v3.main(src_posts=f"{RAW}_posts.csv",
                        src_replies=f"{RAW}_replies.csv",
                        out=str(CLEAN),
                        report=str(REPORT))
    log.info("Clean finished | %s posts / %s replies / %s links | took %.1f min",
             f"{res['posts']:,}", f"{res['replies']:,}", f"{res['links']:,}",
             (time.time() - t0) / 60)
    return res


def step_status():
    st = checkpoint_stats()
    if not st:
        print("No data yet: crawl has never been run.")
        return
    print(f"Checkpoint   {Path(RAW).name}.jsonl  {st['size_mb']} MB  updated {st['mtime']}")
    print(f"Threads      {st['posts']:,} / ~{SITEMAP_TOTAL_HINT:,}"
          f"  ({st['posts'] / SITEMAP_TOTAL_HINT * 100:.1f}%)")
    print(f"Replies      {st['replies']:,}")
    print(f"Newest post  {st['newest_post'][:10]}   newest reply {st['newest_reply'][:10]}")
    print()
    for label, name in (("raw posts", f"{RAW}_posts.csv"),
                        ("raw replies", f"{RAW}_replies.csv"),
                        ("clean posts", f"{CLEAN}_posts.csv"),
                        ("clean replies", f"{CLEAN}_replies.csv"),
                        ("links table", f"{CLEAN}_links.csv")):
        f = Path(name)
        if f.exists():
            print(f"  {label:14} {f.stat().st_size / 1e6:7.1f} MB  "
                  f"{datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M}")
        else:
            print(f"  {label:14}      -  not generated yet")
    if LOCK.exists():
        print(f"\nNote: {LOCK.name} exists, an instance may be running.")


# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(
        description="WomenHeart corpus pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python src/pipeline.py run                    crawl new threads and clean\n"
               "  python src/pipeline.py run --force-clean      re-clean even with no new threads\n"
               "  python src/pipeline.py crawl --target 500     stop after 500 threads\n"
               "  python src/pipeline.py status                 show the current state\n")
    ap.add_argument("command", choices=["run", "crawl", "clean", "status"])
    ap.add_argument("--target", type=int, default=None,
                    help="cumulative target thread count; omit to crawl every thread in "
                         "the sitemap that has not been crawled yet (the normal usage)")
    ap.add_argument("--relevance", choices=["terms", "topic", "off"], default="terms",
                    help="relevance filter mode (default: terms, the cardiovascular lexicon)")
    ap.add_argument("--force-clean", action="store_true",
                    help="on run, clean again even when no new threads were found")
    ap.add_argument("--quiet", action="store_true",
                    help="write to the log file only, do not print to the terminal")
    args = ap.parse_args()

    if args.command == "status":
        step_status()
        return 0

    ensure_dirs()
    setup_logging(verbose=not args.quiet)
    log.info("=" * 58)
    log.info("pipeline %s starting", args.command)

    try:
        with RunLock():
            if args.command == "crawl":
                step_crawl(args.target, args.relevance)
            elif args.command == "clean":
                step_clean()
            else:  # run
                new_p, _ = step_crawl(args.target, args.relevance)
                if new_p or args.force_clean:
                    step_clean()
                else:
                    log.info("No new threads, skipping clean "
                             "(use --force-clean to clean anyway)")
    except KeyboardInterrupt:
        log.warning("Interrupted by user. What was crawled is already in the checkpoint; "
                    "re-running the same command resumes from there.")
        return 130
    except Exception:
        log.exception("Pipeline failed")
        return 1

    log.info("pipeline %s done", args.command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
