"""WomenHeart corpus cleaning pipeline.

Principle: **flag, do not delete**. Apart from PII, no row is dropped and no content is
removed -- whether to exclude staff template replies, self-replies by the original
poster, or very short posts is left to downstream analysis to decide per research
question; this stage only supplies boolean columns.

Input   womenheart_v3_posts.csv / _replies.csv   (the raw files are never modified)
Output  womenheart_clean_posts.csv
        womenheart_clean_replies.csv
        womenheart_clean_links.csv    every link found in the bodies (document level,
                                      full URL preserved)
        clean_report.txt              what this cleaning run did

Cleaning order (there are dependencies -- do not reorder):
  1 fix mojibake  ->  2 normalize Unicode  ->  3 extract links  ->  4 mask emails
  ->  5 mask phone numbers  ->  6 tidy whitespace  ->  7 recompute lengths
Links must be extracted before emails are masked, otherwise the "user@host" shape inside
a URL is bitten in half by the email regex first.
"""

import csv
import hashlib
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import pandas as pd

# Defaults used when running `python src/clean_v3.py` standalone; pipeline.py overrides
# them by passing explicit arguments. This file lives in src/, so the project root is
# one level up.
_HERE = Path(__file__).resolve().parent.parent
SRC_POSTS = str(_HERE / "data" / "raw" / "womenheart_v3_posts.csv")
SRC_REPLIES = str(_HERE / "data" / "raw" / "womenheart_v3_replies.csv")
OUT = str(_HERE / "data" / "clean" / "womenheart_clean")
REPORT = str(_HERE / "data" / "clean" / "clean_report.txt")

# ---------- 1. mojibake ----------
# Common wreckage from UTF-8 that was decoded as cp1252. Fixed by targeted replacement
# rather than a whole-string encode/decode round trip: of the 78 affected documents, only
# 65 round-trip cleanly, and the rest either raise or get corrupted a second time.
MOJIBAKE = [
    ("â€™", "’"), ("â€˜", "‘"), ("â€œ", "“"), ("â€\x9d", "”"),
    ("â€”", "—"), ("â€“", "–"), ("â€¦", "…"), ("â€¢", "•"),
    ("Ã©", "é"), ("Ã¨", "è"), ("Ã¡", "á"), ("Ã¼", "ü"), ("Ã±", "ñ"), ("Ã¶", "ö"),
    ("Â©", "©"), ("Â®", "®"), ("Â°", "°"), ("Â½", "½"), ("Â£", "£"),
    # The last two entries are tail fixes and must stay last, so they cannot
    # eat the multi-character combinations above.
    ("â€", '"'),          # leftover remnant, usually a quote
    ("Â", ""),            # stray remnant, a surplus byte
]

# ---------- 2. Unicode ----------
QUOTES = {"‘": "'", "’": "'", "‚": "'", "‛": "'",
          "“": '"', "”": '"', "„": '"', "‟": '"',
          "′": "'", "″": '"'}
SPACES = {" ": " ", " ": " ", " ": " ", " ": " ",
          " ": " ", "　": " ", "\t": " "}
ZERO_WIDTH = re.compile(r"[​-‍⁠﻿]")
EMOJI_RX = re.compile("[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF☀-➿⬀-⯿]")

# ---------- 3~5. PII ----------
URL_RX = re.compile(r"https?://[^\s<>\"'\)\]}]+", re.IGNORECASE)
BARE_DOMAIN_RX = re.compile(r"\bwww\.[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[^\s<>\"'\)\]}]*)?", re.IGNORECASE)
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
PHONE_RX = re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")

# Consumer mail providers. Everything else is treated as institutional (.org/.edu/.gov
# and the various organisations' own domains).
PERSONAL_MAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "icloud.com", "aol.com", "outlook.com",
    "msn.com", "comcast.net", "me.com", "live.com", "sbcglobal.net", "verizon.net",
    "att.net", "bellsouth.net", "juno.com", "cox.net", "charter.net", "earthlink.net",
    "mac.com", "mail.com", "ymail.com", "rocketmail.com", "gmx.com", "protonmail.com",
}

WS_MULTI_NL = re.compile(r"\n{3,}")
WS_TRAIL = re.compile(r"[ ]+\n")
WS_RUN = re.compile(r"[ ]{2,}")


def domain_of(url):
    m = re.match(r"(?:https?://)?([^/\s:]+)", url, re.IGNORECASE)
    if not m:
        return ""
    d = m.group(1).lower()
    # removeprefix, not lstrip -- lstrip strips a *set of characters*, which would turn
    # www.womenheart.org into omenheart.org (w and . are both in the set).
    return d.removeprefix("www.")


def fix_mojibake(s):
    if "â" not in s and "Ã" not in s and "Â" not in s:
        return s
    for bad, good in MOJIBAKE:
        s = s.replace(bad, good)
    return s


def normalize_unicode(s):
    s = unicodedata.normalize("NFC", s)
    s = ZERO_WIDTH.sub("", s)
    for a, b in SPACES.items():
        s = s.replace(a, b)
    for a, b in QUOTES.items():
        s = s.replace(a, b)
    s = s.replace("…", "...")
    return s


def tidy_ws(s):
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = WS_TRAIL.sub("\n", s)
    s = WS_RUN.sub(" ", s)
    s = WS_MULTI_NL.sub("\n\n", s)
    return s.strip()


def clean_doc(text, doc_id, doc_type, links_out, stats):
    """Return (cleaned text, counter dict). Links are appended to links_out."""
    if not isinstance(text, str):
        text = ""
    orig = text

    text = fix_mojibake(text)
    if text != orig:
        stats["mojibake_docs"] += 1
    text = normalize_unicode(text)

    n_emoji = len(EMOJI_RX.findall(text))

    # --- Links: pull them into the table first, leave the domain in the body ---
    n_url = 0

    def _url(m):
        nonlocal n_url
        u = m.group(0).rstrip(".,;:!?")
        d = domain_of(u)
        links_out.append((doc_id, doc_type, d, u))
        n_url += 1
        return f"[URL:{d}]" if d else "[URL]"

    text = URL_RX.sub(_url, text)
    text = BARE_DOMAIN_RX.sub(_url, text)

    # --- Email addresses ---
    n_email = 0

    def _mail(m):
        nonlocal n_email
        n_email += 1
        dom = m.group(0).split("@", 1)[1].lower().rstrip(".")
        stats["email_domains"][dom] += 1
        return "[EMAIL]" if dom in PERSONAL_MAIL else "[EMAIL:ORG]"

    text = EMAIL_RX.sub(_mail, text)

    # --- Phone numbers ---
    n_phone = len(PHONE_RX.findall(text))
    text = PHONE_RX.sub("[PHONE]", text)

    text = tidy_ws(text)
    return text, dict(n_url=n_url, n_email=n_email, n_phone=n_phone,
                      n_emoji=n_emoji, text_changed=(text != orig))


def author_hash(a):
    return hashlib.sha256(("womenheart::" + str(a)).encode("utf-8")).hexdigest()[:12]


def main(src_posts=SRC_POSTS, src_replies=SRC_REPLIES, out=OUT, report=REPORT):
    print("Reading raw files...", flush=True)
    P = pd.read_csv(src_posts, encoding="utf-8-sig")
    R = pd.read_csv(src_replies, encoding="utf-8-sig")
    print(f"  posts {len(P):,} | replies {len(R):,}", flush=True)

    links = []
    stats = Counter()
    stats["email_domains"] = Counter()

    for df, key, dtype in ((P, "post_id", "post"), (R, "reply_id", "reply")):
        out_text, cols = [], []
        for did, txt in zip(df[key], df["content"]):
            t, c = clean_doc(txt, did, dtype, links, stats)
            out_text.append(t)
            cols.append(c)
        df["content"] = out_text
        meta = pd.DataFrame(cols, index=df.index)
        for c in meta.columns:
            df[c] = meta[c]
        df["word_count"] = df.content.str.split().str.len().fillna(0).astype(int)
        df["char_count"] = df.content.str.len()
        df["author_hash"] = df.author.map(author_hash)
        print(f"  {dtype} cleaned", flush=True)

    # Was this reply written by the original poster?
    opa = P.set_index("post_id").author
    R["is_op"] = (R.author.values == R.post_id.map(opa).values)
    # Is the original post very short?
    P["is_short"] = P.word_count < 20

    L = pd.DataFrame(links, columns=["doc_id", "doc_type", "domain", "url"])

    P.to_csv(f"{out}_posts.csv", index=False, encoding="utf-8-sig",
             quoting=csv.QUOTE_MINIMAL)
    R.to_csv(f"{out}_replies.csv", index=False, encoding="utf-8-sig",
             quoting=csv.QUOTE_MINIMAL)
    L.to_csv(f"{out}_links.csv", index=False, encoding="utf-8-sig")

    rep = []
    rep.append(f"posts   {len(P):,} rows x {P.shape[1]} cols")
    rep.append(f"replies {len(R):,} rows x {R.shape[1]} cols")
    rep.append(f"links   {len(L):,} rows ({L.domain.nunique():,} unique domains)")
    rep.append("")
    rep.append(f"mojibake fixed              {stats['mojibake_docs']:,} documents")
    rep.append(f"links replaced by [URL:dom] {int(P.n_url.sum()+R.n_url.sum()):,} occurrences")
    rep.append(f"emails masked               {int(P.n_email.sum()+R.n_email.sum()):,} occurrences")
    rep.append(f"phone numbers masked        {int(P.n_phone.sum()+R.n_phone.sum()):,} occurrences")
    rep.append(f"text changed                {int(P.text_changed.sum()+R.text_changed.sum()):,} documents")
    rep.append(f"contains emoji              {int((P.n_emoji>0).sum()+(R.n_emoji>0).sum()):,} documents (kept)")
    rep.append("")
    rep.append(f"flagged is_staff            {int(R.is_staff.sum()):,} replies")
    rep.append(f"flagged is_op               {int(R.is_op.sum()):,} replies (by the original poster)")
    rep.append(f"flagged author_inactive     {int(P.author_inactive.sum()+R.author_inactive.sum()):,} documents")
    rep.append(f"flagged is_short            {int(P.is_short.sum()):,} posts (<20 words)")
    rep.append("")
    rep.append("Most shared domains:")
    for d, n in L.domain.value_counts().head(15).items():
        rep.append(f"  {n:5}  {d}")
    txt = "\n".join(rep)
    open(report, "w", encoding="utf-8").write(txt)
    print("\n" + txt)
    return {"posts": len(P), "replies": len(R), "links": len(L), "report": txt}


if __name__ == "__main__":
    main()
