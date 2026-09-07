"""
Inspire.com WomenHeart forum crawler - v3

Changes relative to v2:
  1) Structured extraction: instead of concatenating every <p> on the page into one
     string, each utterance is split out by DOM component --
     original post  ins-intouch-post  -> h1.pb-title / a.pb-topic / div.pb-desc /
                                        a.pb-author-name / span.pb-created-date
     reply          ins-anon-reply    -> span.rhb-name / span.rhb-stamp / <p> /
                                        span.rbw-foot
     This produces two tables, posts and replies, joined on post_id, and every reply
     carries its own author + timestamp. Side effect: the duplicated original post on
     ?p>=2 disappears on its own (the OP is only read from page 1), so prefix-based
     de-duplication is no longer needed.
  2) Keyword filtering now uses a cardiovascular lexicon plus the site's own topic
     taxonomy as a second signal, replacing the literal word "heart" used in v1/v2.
     Measured: strict literal "heart" matching kept only 4/10 threads, the lexicon kept
     10/10 -- domain terms such as HFpEF, angiogram and microvascular do not contain the
     literal word "heart" yet are among the most relevant content. The matched terms are
     written to the relevance_hits field so the filter can be audited and re-applied
     later without re-crawling.
  3) Relevance is decided on page 1 (from the original post alone); irrelevant threads
     skip their reply pages entirely, which saves requests.
  4) Post bodies are no longer truncated.

Pagination (confirmed in v2, carried over here):
  - Listing pages have no pagination links (Angular infinite scroll), so URLs are
    enumerated from the sitemap instead; womenheart has 12,488 threads in total.
  - Reply pagination is ?p=2,3,... and is followed one page at a time.
"""

import re
import csv
import json
import time
import random
import argparse
from pathlib import Path
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

SITEMAP_INDEX = "https://www.inspire.com/sitemap.xml"
SM_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

STAFF_AUTHORS = {"teaminspire", "inspire", "womenheart"}

# The 22 official topics of the WomenHeart group (taken from sitemap_topic.xml).
# All of them fall within the cardiovascular domain.
CARDIAC_TOPIC_SLUGS = {
    "advice-for-other-women-with-heart-disease", "all-about-arrhythmias",
    "atrial-fibrillation", "caregiver-corner", "coronary-artery-disease",
    "emotional-and-psychological-issues", "endocarditis-and-other-heart-valve-problems",
    "experiences-with-alternative-treatments", "experiences-with-healthcare-professionals",
    "experiences-with-medical-treatments", "experiences-with-prescription-medicines",
    "hearttalks-keep-the-conversation-going", "how-heart-disease-has-changed-my-life",
    "living-with-cardiomyopathy-and-congestive-heart-failure",
    "living-with-congenital-heart-disease", "my-heart-disease-diagnosis-and-symptoms",
    "myocardial-infarction-heart-attack", "paroxysmal-supraventricular-tachycardia-psvt",
    "research-and-clinical-trials", "spontaneous-coronary-artery-dissection-scad",
    "understanding-microvascular-disease", "younger-women-with-heart-disease",
}

# Cardiovascular lexicon: conditions / tests and procedures / medications / core symptoms
CARDIAC_TERMS = [
    r"heart (?:disease|attack|failure|condition|problem|issue|surgery|health|murmur|rate)",
    r"cardiac", r"cardiology", r"cardiologist", r"cardiovascular",
    r"myocardial infarction", r"\bMI\b", r"angina", r"prinzmetal", r"ischemi", r"atheroscleros",
    r"atrial fibrillation", r"\bafib\b", r"\ba-fib\b", r"arrhythmi", r"tachycardi", r"bradycardi",
    r"\bPSVT\b", r"palpitation", r"cardiomyopath", r"congestive heart", r"\bCHF\b",
    r"\bHFpEF\b", r"\bHFrEF\b", r"ejection fraction", r"\bEF\b",
    r"coronary", r"\bCAD\b", r"\bSCAD\b", r"microvascular", r"\bMVD\b",
    r"valve", r"mitral", r"aortic", r"tricuspid", r"stenosis", r"regurgitat",
    r"endocarditis", r"pericarditis", r"\batrial\b", r"congenital heart",
    r"\bLVAD\b", r"\bPOTS\b", r"dysautonomia",
    r"stent", r"bypass", r"\bCABG\b", r"angiogram", r"angioplasty", r"catheteriz",
    r"\bcath lab\b", r"\bTAVR\b", r"echocardiogram", r"\becho\b", r"\bEKG\b", r"\bECG\b",
    r"stress test", r"pacemaker", r"\bICD\b", r"defibrillator", r"ablation", r"troponin",
    r"calcium score",
    r"beta.?blocker", r"metoprolol", r"toprol", r"statin", r"nitroglycerin", r"\bnitro\b",
    r"plavix", r"clopidogrel", r"warfarin", r"coumadin", r"eliquis", r"xarelto",
    r"diltiazem", r"amlodipine", r"imdur", r"ranexa", r"entresto", r"amiodarone",
    r"lisinopril", r"blood thinner", r"calcium channel blocker",
    r"chest pain", r"shortness of breath", r"blood pressure",
]
CARDIAC_RX = re.compile("|".join(CARDIAC_TERMS), re.IGNORECASE)

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
STAMP_RX = re.compile(r"([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})\s*[•·]\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
                      re.IGNORECASE)


def parse_stamp(text):
    """'Jan 12, 2012 - 4:11 PM' -> '2012-01-12T16:11:00' (site local time, no timezone)."""
    m = STAMP_RX.search(text or "")
    if not m:
        return ""
    mon, day, year, hh, mm, ap = m.groups()
    mon_n = MONTHS.get(mon.title())
    if not mon_n:
        return ""
    hh = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
    return f"{int(year):04d}-{mon_n:02d}-{int(day):02d}T{hh:02d}:{int(mm):02d}:00"


def txt(node, sep=" "):
    return node.get_text(sep, strip=True) if node else ""


INACTIVE_RX = re.compile(r"\s*\(\s*inactive\s*\)\s*$", re.IGNORECASE)


def split_author(raw):
    """'MabelLean (Inactive)' -> ('MabelLean', True).

    Deactivated accounts carry an (Inactive) suffix on the display name. Without
    splitting it off, the same person counts as two different authors before and after
    deactivation, which corrupts every author-level statistic.
    """
    raw = (raw or "").strip()
    if INACTIVE_RX.search(raw):
        return INACTIVE_RX.sub("", raw).strip(), True
    return raw, False


class WomenHeartCrawler:
    def __init__(self, group="womenheart", target_count=1000, out="womenheart_v3",
                 relevance="terms", max_reply_pages=100,
                 min_delay=1.5, max_delay=3.0, resume=True, write_json=False):
        self.group = group
        # None = no cap: crawl every thread in the sitemap that has not been seen yet.
        # Hard-coding a cap here means that once the target is reached, every later run
        # breaks on the first line of the loop and new threads are never discovered.
        self.target_count = target_count if target_count else float("inf")
        self.out = out
        self.relevance = relevance          # terms | topic | off
        self.max_reply_pages = max_reply_pages
        self.min_delay, self.max_delay = min_delay, max_delay
        self.write_json = write_json

        self.threads = []                   # [{"post": {...}, "replies": [...]}]
        self.seen_urls = set()
        self.skipped_irrelevant = 0
        self.failed = []

        self.jsonl_path = Path(f"{out}.jsonl")
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

        if resume:
            self._load_checkpoint()

    # ---------- Resume from checkpoint ----------

    def _load_checkpoint(self):
        if not self.jsonl_path.exists():
            return
        with self.jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                url = rec["post"]["url"]
                if url not in self.seen_urls:
                    self.seen_urls.add(url)
                    self.threads.append(rec)
        if self.threads:
            print(f"[resume] recovered {len(self.threads)} threads from {self.jsonl_path}")

    def _append_checkpoint(self, rec):
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---------- Networking ----------

    def fetch(self, url, tries=3):
        for attempt in range(tries):
            try:
                time.sleep(random.uniform(self.min_delay, self.max_delay))
                resp = self.session.get(url, timeout=25)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code == 404:
                    return None
                print(f"    [HTTP {resp.status_code}] {url}")
            except Exception as e:
                print(f"    [!] request failed ({attempt + 1}/{tries}): {e}")
            time.sleep(2 ** attempt)
        return None

    # ---------- Sitemap enumeration ----------

    def iter_discussion_urls(self):
        print(f"[sitemap] reading index {SITEMAP_INDEX}")
        index_xml = self.fetch(SITEMAP_INDEX)
        if not index_xml:
            raise RuntimeError("could not fetch the sitemap index")

        root = ElementTree.fromstring(index_xml)
        shard_re = re.compile(rf"sitemap_disc_{re.escape(self.group)}__\d+\.xml$")
        shards = [loc.text.strip()
                  for loc in root.findall(".//s:sitemap/s:loc", SM_NS)
                  if loc.text and shard_re.search(loc.text.strip())]
        print(f"[sitemap] matched {len(shards)} shards")

        entries = []
        for shard in shards:
            xml = self.fetch(shard)
            if not xml:
                print(f"[sitemap] failed to read shard: {shard}")
                continue
            sroot = ElementTree.fromstring(xml)
            n = 0
            for url_el in sroot.findall(".//s:url", SM_NS):
                loc = url_el.find("s:loc", SM_NS)
                if loc is None or not loc.text or "/discussion/" not in loc.text:
                    continue
                mod = url_el.find("s:lastmod", SM_NS)
                entries.append((loc.text.strip(),
                                mod.text.strip() if mod is not None and mod.text else ""))
                n += 1
            print(f"[sitemap] {shard.rsplit('/', 1)[-1]}: {n} entries")

        entries.sort(key=lambda t: t[1], reverse=True)
        print(f"[sitemap] {len(entries)} discussion URLs total, newest lastmod first\n")
        return entries

    # ---------- Structured extraction ----------

    @staticmethod
    def _post_id(url):
        return url.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def is_staff(author):
        return author.strip().lower() in STAFF_AUTHORS

    def parse_op(self, soup, url, lastmod):
        box = soup.find("ins-intouch-post") or soup.find("app-individual-post")
        if not box:
            return None

        topic_a = box.find("a", class_="pb-topic")
        topic_slug = ""
        if topic_a and topic_a.get("href"):
            topic_slug = topic_a["href"].rstrip("/").rsplit("/", 1)[-1]

        stamp = txt(box.find("span", class_="pb-created-date"))
        # Do not constrain this to <a>. Deactivated users have no profile link, so their
        # pb-author-name is a span rather than an a, and constraining the tag would
        # extract an empty author for exactly those accounts.
        author, inactive = split_author(txt(box.find(class_="pb-author-name")))
        body = txt(box.find("div", class_="pb-desc"), sep="\n\n")
        title = txt(box.find("h1", class_="pb-title"))

        return {
            "post_id": self._post_id(url),
            "url": url,
            "title": title,
            "topic": txt(topic_a),
            "topic_slug": topic_slug,
            "author": author,
            "author_inactive": inactive,
            "is_staff": self.is_staff(author),
            "publish_time": stamp,
            "publish_time_iso": parse_stamp(stamp),
            "lastmod": lastmod,
            "content": body,
            "word_count": len(body.split()),
            "char_count": len(body),
        }

    def parse_replies(self, soup, post_id, url, page_no):
        out = []
        for box in soup.find_all("ins-anon-reply"):
            author, inactive = split_author(txt(box.find(class_="rhb-name")))
            stamp = txt(box.find("span", class_="rhb-stamp"))
            body = "\n\n".join(txt(p) for p in box.find_all("p") if txt(p))
            if not body:
                continue
            foot = txt(box.find("span", class_="rbw-foot"))
            rm = re.search(r"(\d+)", foot)
            out.append({
                "post_id": post_id,
                "post_url": url,
                "page_no": page_no,
                "author": author,
                "author_inactive": inactive,
                "is_staff": self.is_staff(author),
                "publish_time": stamp,
                "publish_time_iso": parse_stamp(stamp),
                "content": body,
                "word_count": len(body.split()),
                "reactions": int(rm.group(1)) if rm else 0,
            })
        return out

    # ---------- Relevance ----------

    def relevance_of(self, post):
        """Return (keep?, matched terms). Only the original post is inspected, so an
        off-topic thread cannot be dragged in by something a reply happens to say."""
        if self.relevance == "off":
            return True, []
        if self.relevance == "topic":
            return post["topic_slug"] in CARDIAC_TOPIC_SLUGS, []
        hits = sorted({m.group(0).lower() for m in
                       CARDIAC_RX.finditer(f"{post['title']} {post['content']}")})
        # Threads filed under an official cardiovascular topic are kept even when the
        # body matches no lexicon term.
        keep = bool(hits) or post["topic_slug"] in CARDIAC_TOPIC_SLUGS
        return keep, hits

    # ---------- Single-thread crawl ----------

    @staticmethod
    def _links_to_page(soup, url, want):
        """Does the current page link to page `want` of this thread?

        The pager is a **sliding window**, not the full set: page 1 lists only ?p=2..8,
        9 does not appear until you reach page 8, and page 20 shows 16..23. So the total
        page count cannot be computed from page 1 -- doing that truncates every thread
        longer than 8 pages (120 replies). Pages must be advanced one at a time, with the
        window re-read on each. Only links pointing at this thread's own path count, so
        stray ?p= parameters elsewhere on the page cannot leak in.
        """
        path = "/" + url.split("//", 1)[-1].split("/", 1)[-1].rstrip("/")
        for a in soup.find_all("a", href=True):
            m = re.search(r"\?p=(\d+)$", a["href"])
            if m and int(m.group(1)) == want and \
                    a["href"].rstrip("/").split("?")[0].rstrip("/") == path:
                return True
        return False

    @staticmethod
    def _reported_replies(soup):
        """Read the site's own reply count from div.number-of-replies.

        Regexing the whole page text for "N replies" does not work -- it matches ordinary
        sentences in the post body. (Observed: the "sisters" thread says "If you get 5
        replies..." in its body, and 5 was picked up as the count.)
        """
        node = soup.find("div", class_="number-of-replies")
        m = re.search(r"(\d+)", txt(node)) if node else None
        return int(m.group(1)) if m else 0

    def crawl_thread(self, url, lastmod=""):
        html = self.fetch(url)
        if not html:
            self.failed.append(url)
            return None

        soup = BeautifulSoup(html, "html.parser")
        post = self.parse_op(soup, url, lastmod)
        if not post:
            self.failed.append(url)
            return None

        # The server occasionally returns a partially rendered page (the post container
        # is present but the body is empty). Left unhandled, such a page matches no
        # lexicon term and is silently dropped as "irrelevant" -- 4 clearly
        # cardiovascular older threads were lost this way. Refetch once; if the body is
        # still empty, count it as a crawl failure rather than as irrelevant.
        if not (post["content"] or "").strip():
            retry = self.fetch(url)
            if retry:
                soup = BeautifulSoup(retry, "html.parser")
                again = self.parse_op(soup, url, lastmod)
                if again and (again["content"] or "").strip():
                    post = again
        if not (post["content"] or "").strip():
            print(f"    [empty body] {url}")
            self.failed.append(url)
            return None

        keep, hits = self.relevance_of(post)
        if not keep:
            self.skipped_irrelevant += 1
            return None
        post["relevance_hits"] = "|".join(hits)

        replies = self.parse_replies(soup, post["post_id"], url, 1)
        reported = self._reported_replies(soup)
        pages_fetched = 1
        cur = soup                      # current page, used to read the next-page window

        # Two criteria, OR'd: the window points at the next page, or we have not yet
        # collected as many replies as the site reports.
        # The window alone is not enough -- the server occasionally renders page 1's
        # pager onto a middle page (observed: page 15 of i-think-maybe-i-am-dying came
        # back with [2..8]), which cuts long threads off midway.
        # The count alone is not enough either -- reported can be 0. Past the last page
        # the site returns 404, which is a natural stop condition.
        while pages_fetched < self.max_reply_pages:
            nxt = pages_fetched + 1
            if not self._links_to_page(cur, url, nxt) and len(replies) >= reported:
                break
            page_html = self.fetch(f"{url}?p={nxt}")
            if not page_html:                       # 404 = past the last page
                break
            cur = BeautifulSoup(page_html, "html.parser")
            got = self.parse_replies(cur, post["post_id"], url, nxt)
            if not got:
                break
            replies.extend(got)
            pages_fetched = nxt

        # No cross-page de-duplication: v2 needed it because ?p>=2 repeated the original
        # post, but v3 reads the OP from page 1 only, so duplicates cannot occur
        # structurally. Keeping it would instead delete genuine duplicate posts by users
        # (observed: on angioplasty-plavix someone posted identical content twice within
        # the same minute, and the site's own count includes both).
        for i, r in enumerate(replies, 1):
            r["seq"] = i
            r["reply_id"] = f"{post['post_id']}#{i}"

        post["replies_reported"] = reported
        post["replies_scraped"] = len(replies)
        post["reply_pages_fetched"] = pages_fetched
        # Pages are advanced one at a time until the window stops offering a next page,
        # so the number walked is the total; the only case where it is not is when the
        # max_reply_pages cap was hit.
        post["reply_pages_total"] = pages_fetched
        post["hit_page_cap"] = pages_fetched >= self.max_reply_pages

        return {"post": post, "replies": replies}

    # ---------- Main loop ----------

    def run(self):
        entries = self.iter_discussion_urls()
        before = len(self.threads)

        for url, lastmod in entries:
            if len(self.threads) >= self.target_count:
                break
            if url in self.seen_urls:
                continue
            self.seen_urls.add(url)

            cap = "all" if self.target_count == float("inf") else self.target_count
            print(f"[{len(self.threads)}/{cap}] {self._post_id(url)[:58]}")
            rec = self.crawl_thread(url, lastmod)
            if not rec:
                continue

            self.threads.append(rec)
            self._append_checkpoint(rec)
            p = rec["post"]
            flag = "" if p["replies_scraped"] >= p["replies_reported"] \
                else "  <-- fewer replies than reported"
            print(f"    OK | {p['author'] or '?'} | {p['topic'][:28]} | "
                  f"replies {p['replies_scraped']}/{p['replies_reported']} | "
                  f"pages {p['reply_pages_fetched']}/{p['reply_pages_total']}{flag}")

        n_rep = sum(len(t["replies"]) for t in self.threads)
        print(f"\n{'=' * 60}")
        print(f"Added {len(self.threads) - before} threads this run; "
              f"{len(self.threads)} threads / {n_rep} replies in total")
        print(f"Skipped by relevance filter: {self.skipped_irrelevant} | "
              f"failed: {len(self.failed)}")
        return self.threads

    # ---------- Persistence ----------

    def save(self):
        if not self.threads:
            print("Nothing to save")
            return

        posts = [t["post"] for t in self.threads]
        replies = [r for t in self.threads for r in t["replies"]]

        post_fields = ["post_id", "url", "title", "topic", "topic_slug", "author", "author_inactive", "is_staff",
                       "publish_time", "publish_time_iso", "lastmod", "content", "word_count",
                       "char_count", "replies_reported", "replies_scraped",
                       "reply_pages_total", "reply_pages_fetched", "hit_page_cap", "relevance_hits"]
        reply_fields = ["reply_id", "post_id", "post_url", "seq", "page_no", "author",
                        "author_inactive", "is_staff", "publish_time", "publish_time_iso", "content",
                        "word_count", "reactions"]

        for name, rows, fields in (("posts", posts, post_fields),
                                   ("replies", replies, reply_fields)):
            with open(f"{self.out}_{name}.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            # On the full corpus, indented JSON runs to well over a hundred megabytes,
            # is slow to write and is essentially never read; the line-oriented .jsonl
            # checkpoint already serves as the structured backup. Pass --json to
            # re-enable it when needed.
            if self.write_json:
                with open(f"{self.out}_{name}.json", "w", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False, indent=2)
            suffix = " / .json" if self.write_json else ""
            print(f"Saved {self.out}_{name}.csv{suffix} ({len(rows)} rows)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="womenheart")
    ap.add_argument("--target", type=int, default=None,
                    help="cumulative target thread count; omit to crawl every thread in "
                         "the sitemap that has not been crawled yet")
    ap.add_argument("--out",
                    default=str(Path(__file__).resolve().parent.parent
                                / "data" / "raw" / "womenheart_v3"),
                    help="output prefix (without extension)")
    ap.add_argument("--relevance", choices=["terms", "topic", "off"], default="terms",
                    help="terms=cardiovascular lexicon (default)  "
                         "topic=official topics only  off=no filtering")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--json", action="store_true",
                    help="also write indented JSON (~137MB on the full corpus; off by default)")
    args = ap.parse_args()

    crawler = WomenHeartCrawler(group=args.group, target_count=args.target, out=args.out,
                                relevance=args.relevance, resume=not args.no_resume,
                                write_json=args.json)
    crawler.run()
    crawler.save()
