# WomenHeart Inspire Pipeline

Collects discussion threads and replies from the [WomenHeart](https://www.inspire.com/groups/womenheart/)
support community on Inspire.com and cleans them into an analysis-ready corpus of
heart-health discussion.

This repository contains **code only**. No scraped data, logs or exports are committed —
`data/` and `logs/` are git-ignored and ship empty. Run the pipeline to build the corpus
yourself.

> **Reference figures.** All corpus numbers in this README come from one full run
> completed in September 2026: **12,488 threads / 103,625 replies / ~14.58M words**,
> spanning 2006–2026. They describe that snapshot, not anything stored in this
> repository, and a fresh run will differ.

---

## Requirements

- Python 3.9 or newer (uses `str.removeprefix`); developed and tested on 3.12
- `requests`, `beautifulsoup4`, `pandas`

```bash
pip install -r requirements.txt
```

## Quick start

```bash
python src/pipeline.py status          # show the current state of the corpus
python src/pipeline.py run             # crawl new threads -> clean (the everyday command)
```

`run` skips the cleaning step when no new threads were found, so repeating it is cheap
(~25 seconds, almost all of it spent reading the sitemap).

A full crawl from an empty checkpoint takes roughly **13 hours** at the built-in request
rate. It is interruptible and resumable — see [Data flow](#data-flow).

### All commands

| Command | Effect |
|---|---|
| `python src/pipeline.py run` | Crawl new threads, then clean if anything was added |
| `python src/pipeline.py run --force-clean` | Clean even when no new threads were found |
| `python src/pipeline.py crawl` | Crawl only |
| `python src/pipeline.py clean` | Clean only (re-runs on the existing raw CSVs) |
| `python src/pipeline.py status` | Corpus size, newest content date, file timestamps |

Common options: `--target N` caps the cumulative thread count (useful for debugging),
`--relevance {terms,topic,off}` switches the relevance filter, `--quiet` writes to the
log file without printing to the terminal.

---

## Data flow

```
Inspire sitemap
      │  enumerates every discussion URL in the group
      │  (the only entry point robots.txt permits)
      ▼
data/raw/womenheart_v3.jsonl        ← source of truth. One line appended per thread;
      │                               this is what makes the crawl resumable
      │
      ├──► data/raw/womenheart_v3_posts.csv       raw, contains real PII,
      │    data/raw/womenheart_v3_replies.csv     internal provenance only
      │
      └──► data/clean/womenheart_clean_posts.csv    de-identified — use these for
           data/clean/womenheart_clean_replies.csv  analysis and for anything shared
           data/clean/womenheart_clean_links.csv    full URLs found in the bodies
           data/clean/clean_report.txt              what this cleaning run did
```

**Interrupt-safe:** the `.jsonl` is appended one thread at a time, so after a power loss
or Ctrl+C, re-running the same command resumes from where it stopped. To force a complete
re-crawl, delete the `.jsonl`.

---

## Incremental semantics (important)

**New threads are discovered; new replies on old threads are not.**

Threads that have already been crawled are never revisited, so replies added later to an
older thread never enter the corpus. This is a deliberate trade-off: over a measured
30-day window only 3 threads in this community saw new activity, and the group averages
about 2 new threads per month, which does not justify maintaining a `lastmod` comparison
just to chase incremental replies.

To refresh replies on old threads, delete the `.jsonl` and re-crawl in full (~13 hours).

### Is this community worth crawling on a schedule?

Over the trailing 12 months of the reference snapshot, the community added a total of
**20 threads, 107 replies and roughly 10,000 words** — about **0.07%** of the existing
corpus. The site's own "New"-sorted front page spanned nearly four months across its
10 most recent threads.

Conclusion: **do not put this on an automatic schedule.** A manual run once a quarter is
enough. If you do want it scheduled, see the next section.

---

## Running on a schedule

Replace `<path-to>` with wherever you cloned this repository.

**Windows Task Scheduler**

1. Open Task Scheduler → Create Basic Task
2. Trigger: your choice of cadence — **quarterly** or monthly is recommended
3. Action → Start a program:
   - Program: `python` (or the full path to your interpreter)
   - Arguments: `src/pipeline.py run --quiet`
   - Start in: `<path-to>\womenheart-inspire-pipeline`

**cron (Linux / macOS)** — 03:00 on the first day of each quarter:

```cron
0 3 1 1,4,7,10 * cd <path-to>/womenheart-inspire-pipeline && python src/pipeline.py run --quiet
```

Every path is anchored to the project root, so the pipeline still runs correctly if the
working directory is wrong; setting it correctly just keeps the logs where you expect
them.

Logs are written to `logs/pipeline_YYYYMMDD.log`. `--quiet` writes only to that file and
prints nothing, which suits background execution.

**Concurrency protection:** a `.pipeline.lock` file is created while running and a second
instance refuses to start, so two processes cannot append interleaved bad lines to the
`.jsonl`. A stale lock left behind by a crashed process is detected and taken over on the
next run.

---

## Repository layout

```
womenheart-inspire-pipeline/
├── README.md
├── requirements.txt
├── .gitignore
├── src/
│   ├── pipeline.py       entry point: orchestration, logging, run lock, status
│   ├── scraper_v3.py     collection engine: sitemap enumeration, reply pagination,
│   │                     structured extraction
│   └── clean_v3.py       cleaning engine: mojibake, Unicode, PII masking, derived flags
├── data/                 git-ignored, ships empty
│   ├── raw/              raw output (contains PII, internal provenance) + .jsonl checkpoint
│   └── clean/            cleaned output (de-identified, for analysis)
└── logs/                 git-ignored — pipeline_YYYYMMDD.log
```

---

## Output fields

### posts (27 columns; the key ones)

`post_id` `url` `title` `topic` `topic_slug` `author` `author_hash` `publish_time_iso`
`content` `word_count` `replies_scraped` `replies_reported` `relevance_hits`
`is_staff` `author_inactive` `is_short` `n_url` `n_email` `n_phone` `n_emoji`

### replies (21 columns; the key ones)

`reply_id` `post_id` (foreign key) `seq` `author` `author_hash` `publish_time_iso`
`content` `word_count` `reactions` `is_staff` `is_op` `author_inactive`

### links

`doc_id` `doc_type` `domain` `url` — links that were replaced by `[URL:domain]` in the
body text; the full address is preserved here.

---

## What the cleaning step does

**Principle: flag, do not delete.** Apart from PII, no row is dropped and no content is
removed. Whether to exclude staff template replies, self-replies by the original poster,
or very short posts is a decision for downstream analysis, driven by the research
question — the pipeline only supplies the boolean columns.

| Step | Detail |
|---|---|
| Mojibake repair | UTF-8 wreckage decoded as cp1252 (`â€™` → `’`); ~81 documents |
| Unicode normalization | NFC; curly quotes → straight; ellipsis → `...`; zero-width characters and 7 kinds of exotic space removed |
| Links | Extracted into the `links` table with the full URL preserved; body text gets `[URL:domain]` |
| Emails | Consumer domains → `[EMAIL]`, institutional domains (.org/.edu etc.) → `[EMAIL:ORG]` |
| Phone numbers | → `[PHONE]` |
| Emoji | **Kept**, with an `n_emoji` count added |
| Whitespace | Trailing spaces, runs of spaces, and 3+ consecutive newlines normalized |

Word count changes by −0.00% across cleaning: nothing is lost except PII.

### Common downstream subsets

```python
import pandas as pd
R = pd.read_csv("data/clean/womenheart_clean_replies.csv", encoding="utf-8-sig")

R[~R.is_staff]                      # exclude staff template replies      102,687
R[~R.is_staff & ~R.is_op]           # also exclude the OP's own replies    80,354
R[pd.to_datetime(R.publish_time_iso).dt.year >= 2017]   # Reactions usable  7,077
```

---

## Three things to know before analysing this data

1. **The timezone is unconfirmed.** Timestamps are the site's rendered local time, and it
   is not known whether the baseline is UTC or US Eastern. Any hour-of-day conclusion
   (for example "3.6× more posting late at night than in the morning") must not go into a
   paper until the timezone is pinned down.

2. **Reactions only launched in 2017.** Before 2016, only 1–3% of replies carry
   reactions; in 2017 that jumps to 40%. Using the field across years misreads *era* as
   *content quality*. Restrict it to the post-2017 subset.

3. **`author` is a pseudonym, but it is reversible.** Combined with `url` it locates the
   member's profile page and their entire posting history. For anything shared externally,
   replace `author` with `author_hash` and drop `url`.

---

## Compliance and ethics

- Collection goes through the site's own sitemap, which robots.txt explicitly permits.
  Its `Disallow: /api/ /search* */reply/` rules mean internal endpoints are off-limits,
  and this code does not touch them.
- Requests are single-threaded with a 1.5–3.0 second delay (~18 requests/minute).
- The content includes specific diagnoses, medications, surgical histories and ages, so it
  is more sensitive than typical forum data. The raw CSVs are **not** de-identified and
  exist for internal provenance only. Use the cleaned files for any external output, and
  confirm whether your institution's ethics process requires this collection to be
  registered.
