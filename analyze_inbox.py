#!/usr/bin/env python3
"""
Thunderbird All Mail analyzer — Gmail inbox reduction toolkit.

Usage:
    python3 analyze_inbox.py [--report REPORT] [--top N] [--year YEAR]
                             [--mbox PATH] [--generate-cache]
                             [--older-than DURATION]
                             [--my-email EMAIL]
                             [--min-age YEARS] [--min-size KB]

Reports:
    default      - curated inbox-reduction report (recommended starting point)
    summary      - message count, date range, top senders and subject keywords
    senders      - top senders and domains by message count and storage
    unsubscribe  - high-volume senders with List-Unsubscribe (safe bulk-delete targets)
    attachments  - top senders by attachment storage
    age-size     - old and large messages (controlled by --min-age and --min-size)
    threads      - top email threads by total storage
    never-replied - senders you have never written back to
    timeline     - messages per month
    subjects     - top subject line words
    largest      - largest individual messages
    all          - run all reports

Caching:
    The first run parses the raw mbox and writes a small cache to
    ~/.analyze_inbox/<folder>.cache. Subsequent runs load from cache
    and complete in under a second. The cache is automatically regenerated
    when it is missing or older than --older-than (default: 24 hours).
"""

import mailbox
import email.header
import email.utils
import argparse
import collections
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

def _i(s):
    """Intern a string to deduplicate repeated values (e.g. sender addresses)."""
    return sys.intern(s) if s else s

CACHE_VERSION = 4


# ---------------------------------------------------------------------------
# Thunderbird profile auto-detection
# ---------------------------------------------------------------------------

def find_thunderbird_profile():
    profiles_dir = Path.home() / "Library/Thunderbird/Profiles"
    if not profiles_dir.exists():
        return None
    candidates = sorted(profiles_dir.iterdir(),
                        key=lambda p: ("default-release" not in p.name, p.name))
    return candidates[0] if candidates else None


THUNDERBIRD = find_thunderbird_profile()
ALL_MAIL = THUNDERBIRD / "ImapMail/imap.gmail.com/[Gmail].sbd/All Mail" if THUNDERBIRD else None
CACHE_DIR = Path.home() / ".analyze_inbox"


# ---------------------------------------------------------------------------
# Cache path
# ---------------------------------------------------------------------------

def cache_path_for(mbox_path):
    CACHE_DIR.mkdir(exist_ok=True)
    stem = Path(mbox_path).name.replace(" ", "_")
    return CACHE_DIR / f"{stem}.cache"


# ---------------------------------------------------------------------------
# Header decoding helpers
# ---------------------------------------------------------------------------

def decode_header(raw):
    if not raw:
        return ""
    parts = []
    for fragment, charset in email.header.decode_header(raw):
        if isinstance(fragment, bytes):
            try:
                parts.append(fragment.decode(charset or "utf-8", errors="replace"))
            except Exception:
                parts.append(fragment.decode("latin-1", errors="replace"))
        else:
            parts.append(fragment)
    return "".join(parts).strip()


def parse_date(msg):
    raw = msg.get("Date", "")
    try:
        t = email.utils.parsedate_to_datetime(raw)
        return t.astimezone(timezone.utc)
    except Exception:
        return None


def sender_address(msg):
    raw = decode_header(msg.get("X-Original-From") or msg.get("From", ""))
    _, addr = email.utils.parseaddr(raw)
    return addr.lower() if addr else raw.lower()


def sender_display(msg):
    raw = decode_header(msg.get("X-Original-From") or msg.get("From", ""))
    name, addr = email.utils.parseaddr(raw)
    if name:
        return f"{name} <{addr.lower()}>"
    return addr.lower()


def primary_to_addr(msg):
    raw = decode_header(msg.get("To", ""))
    # To: may contain multiple addresses; take the first
    for name, addr in email.utils.getaddresses([raw]):
        if addr:
            return addr.lower()
    return ""


def get_thread_id(msg):
    """Return a stable thread identifier from References, In-Reply-To, or Message-ID."""
    refs = (msg.get("References") or "").strip()
    if refs:
        return refs.split()[0].strip("<>")
    irt = (msg.get("In-Reply-To") or "").strip().strip("<>")
    if irt:
        return irt
    mid = (msg.get("Message-ID") or "").strip().strip("<>")
    return mid


def get_list_id(msg):
    """Return the clean List-ID value, e.g. 'list-name.example.com', or ''."""
    raw = decode_header(msg.get("List-ID") or "")
    if not raw:
        return ""
    # Header is often: "Human Name <list-id.domain.com>" — extract the bracketed part
    m = re.search(r"<([^>]+)>", raw)
    return _i(m.group(1).strip()) if m else _i(raw.strip())


def get_attachment_info(msg):
    """Return (has_attachment: bool, attachment_bytes: int)."""
    has_att = False
    att_bytes = 0
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            cd = (part.get("Content-Disposition") or "").lower()
            ct = part.get_content_type()
            if "attachment" in cd or (
                not ct.startswith("text/") and not ct.startswith("multipart/")
            ):
                payload = part.get_payload(decode=True)
                if payload:
                    has_att = True
                    att_bytes += len(payload)
    return has_att, att_bytes


_STOPWORDS = {
    "the", "and", "for", "you", "your", "with", "this", "from", "that",
    "are", "not", "but", "have", "has", "was", "our", "get", "all", "more",
    "can", "its", "new", "now", "will", "just", "out", "off", "about",
    "what", "when", "how", "here", "been", "their", "there", "via", "per",
}


def subject_words(subject):
    s = re.sub(r"^(re|fwd?|fw):\s*", "", subject.lower(), flags=re.I)
    return [w for w in re.findall(r"[a-z]{3,}", s) if w not in _STOPWORDS]


def domain_of(addr):
    return addr.split("@", 1)[-1] if "@" in addr else addr


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

def parse_duration(s):
    """Parse '6 minutes', '24 hours', '3 weeks', etc. into seconds."""
    m = re.fullmatch(r"(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)", s.strip(), re.I)
    if not m:
        raise argparse.ArgumentTypeError(
            f"Cannot parse duration: {s!r}. Use e.g. '24 hours', '6 minutes', '3 weeks'."
        )
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("m"):   return n * 60
    if unit.startswith("h"):   return n * 3600
    if unit.startswith("d"):   return n * 86400
    if unit.startswith("w"):   return n * 604800


# ---------------------------------------------------------------------------
# Cache read / write
# ---------------------------------------------------------------------------

def write_cache(mbox_path, messages):
    mtime = Path(mbox_path).stat().st_mtime
    header = {
        "version": CACHE_VERSION,
        "mbox": str(mbox_path),
        "mbox_mtime": mtime,
        "count": len(messages),
    }
    cache = cache_path_for(mbox_path)
    with open(cache, "w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for m in messages:
            row = {
                "date": m["date"].timestamp() if m["date"] else None,
                "from_addr": m["from_addr"],
                "from_display": m["from_display"],
                "to_addr": m["to_addr"],
                "subject": m["subject"],
                "size": m["size"],
                "has_attachment": m["has_attachment"],
                "attachment_size": m["attachment_size"],
                "list_unsubscribe": m["list_unsubscribe"],
                "list_id": m["list_id"],
                "thread_id": m["thread_id"],
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  Cache written to {cache} ({len(messages):,} messages).", file=sys.stderr)


def read_cache(mbox_path):
    cache = cache_path_for(mbox_path)
    if not cache.exists():
        return None
    mtime = Path(mbox_path).stat().st_mtime
    messages = []
    with open(cache, "r", encoding="utf-8") as f:
        header = json.loads(f.readline())
        if header.get("version", 1) < CACHE_VERSION:
            print(
                f"  Cache is outdated (v{header.get('version',1)} < v{CACHE_VERSION}) — regenerating ...",
                file=sys.stderr,
            )
            return None
        if abs(header.get("mbox_mtime", 0) - mtime) > 1:
            print(
                "  WARNING: mbox has changed since cache was built "
                "(re-run with --generate-cache to refresh).",
                file=sys.stderr,
            )
        for line in f:
            row = json.loads(line)
            row["date"] = datetime.fromtimestamp(row["date"], tz=timezone.utc) if row["date"] else None
            row["from_addr"]    = _i(row["from_addr"])
            row["from_display"] = _i(row["from_display"])
            row["to_addr"]      = _i(row["to_addr"])
            row["list_id"]      = _i(row.get("list_id", ""))
            row["thread_id"]    = _i(row["thread_id"])
            messages.append(row)
    print(f"  Loaded {len(messages):,} messages from cache.", file=sys.stderr)
    return messages


# ---------------------------------------------------------------------------
# Parse mbox (live)
# ---------------------------------------------------------------------------

def parse_mbox(mbox_path, year=None):
    print(f"Parsing {mbox_path} ...", file=sys.stderr)
    mbox = mailbox.mbox(str(mbox_path), factory=None, create=False)
    messages = []
    skipped = 0
    for i, msg in enumerate(mbox):
        if i % 10000 == 0 and i > 0:
            print(f"  {i} messages read...", file=sys.stderr)
        dt = parse_date(msg)
        if year and (dt is None or dt.year != year):
            skipped += 1
            continue
        has_att, att_bytes = get_attachment_info(msg)
        messages.append({
            "date": dt,
            "from_addr": sender_address(msg),
            "from_display": sender_display(msg),
            "to_addr": primary_to_addr(msg),
            "subject": decode_header(msg.get("Subject", "(no subject)")),
            "size": len(bytes(msg)),
            "has_attachment": has_att,
            "attachment_size": att_bytes,
            "list_unsubscribe": bool(msg.get("List-Unsubscribe")),
            "list_id": get_list_id(msg),
            "thread_id": get_thread_id(msg),
        })
    print(
        f"  Parsed {len(messages):,} messages{f' (skipped {skipped})' if skipped else ''}.",
        file=sys.stderr,
    )
    return messages


def load_messages(mbox_path, year=None, generate_cache=False, older_than=86400):
    cache = cache_path_for(mbox_path)

    if not generate_cache and cache.exists():
        age = time.time() - cache.stat().st_mtime
        built_at = datetime.fromtimestamp(cache.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        if age > older_than:
            print(
                f"  Cache built {built_at} ({age/3600:.1f}h ago) — exceeds threshold of {older_than/3600:.1f}h, regenerating ...",
                file=sys.stderr,
            )
            generate_cache = True
        else:
            print(
                f"  Cache built {built_at} ({age/3600:.1f}h ago).",
                file=sys.stderr,
            )

    if generate_cache or not cache.exists():
        if not generate_cache:
            print(f"  No cache found — building {cache} ...", file=sys.stderr)
        messages = parse_mbox(mbox_path, year=year)
        write_cache(mbox_path, messages)
        return messages

    cached = read_cache(mbox_path)
    if cached is None:
        # Version mismatch — regenerate
        messages = parse_mbox(mbox_path, year=year)
        write_cache(mbox_path, messages)
        return messages

    if year:
        cached = [m for m in cached if m["date"] and m["date"].year == year]
    return cached


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def report_summary(messages, top_n):
    dated = [m for m in messages if m["date"]]
    oldest = min(m["date"] for m in dated) if dated else None
    newest = max(m["date"] for m in dated) if dated else None
    total_size = sum(m["size"] for m in messages)
    with_att = sum(1 for m in messages if m["has_attachment"])

    print("\n=== SUMMARY ===")
    print(f"  Total messages   : {len(messages):,}")
    print(f"  Total size       : {total_size / 1_048_576:.1f} MB")
    print(f"  With attachments : {with_att:,} ({100*with_att/len(messages):.1f}%)")
    if oldest:
        print(f"  Oldest           : {oldest.strftime('%Y-%m-%d')}")
        print(f"  Newest           : {newest.strftime('%Y-%m-%d')}")

    counter = collections.Counter(m["from_addr"] for m in messages)
    display_map = {m["from_addr"]: m["from_display"] for m in messages}
    print(f"\n  Top {top_n} senders:")
    for addr, count in counter.most_common(top_n):
        print(f"    {count:5,}  {display_map[addr]}")

    word_counter = collections.Counter()
    for m in messages:
        word_counter.update(subject_words(m["subject"]))
    print(f"\n  Top {top_n} subject words:")
    for word, count in word_counter.most_common(top_n):
        print(f"    {count:5,}  {word}")


def report_senders(messages, top_n):
    count_by_addr = collections.Counter(m["from_addr"] for m in messages)
    size_by_addr = collections.defaultdict(int)
    count_by_domain = collections.Counter()
    size_by_domain = collections.defaultdict(int)
    display_map = {}
    for m in messages:
        addr = m["from_addr"]
        domain = domain_of(addr)
        size_by_addr[addr] += m["size"]
        count_by_domain[domain] += 1
        size_by_domain[domain] += m["size"]
        if addr not in display_map:
            display_map[addr] = m["from_display"]

    total_count = len(messages)
    total_size = sum(size_by_addr.values())

    print(f"\n=== TOP {top_n} SENDERS BY MESSAGE COUNT ===")
    print(f"  {'COUNT':>6}  {'PCT':>5}  {'SENDER'}")
    print(f"  {'-'*6}  {'-'*5}  {'-'*50}")
    for addr, count in count_by_addr.most_common(top_n):
        pct = 100 * count / total_count
        print(f"  {count:6,}  {pct:4.1f}%  {display_map[addr]}")

    print(f"\n=== TOP {top_n} SENDERS BY STORAGE ===")
    print(f"  {'MB':>8}  {'PCT':>5}  {'COUNT':>6}  {'SENDER'}")
    print(f"  {'-'*8}  {'-'*5}  {'-'*6}  {'-'*50}")
    for addr, total_bytes in sorted(size_by_addr.items(), key=lambda x: x[1], reverse=True)[:top_n]:
        mb = total_bytes / 1_048_576
        pct = 100 * total_bytes / total_size
        count = count_by_addr[addr]
        print(f"  {mb:8.1f}  {pct:4.1f}%  {count:6,}  {display_map[addr]}")

    print(f"\n=== TOP {top_n} DOMAINS BY MESSAGE COUNT ===")
    print(f"  {'COUNT':>6}  {'PCT':>5}  {'MB':>8}  {'DOMAIN'}")
    print(f"  {'-'*6}  {'-'*5}  {'-'*8}  {'-'*40}")
    for domain, count in count_by_domain.most_common(top_n):
        pct = 100 * count / total_count
        mb = size_by_domain[domain] / 1_048_576
        print(f"  {count:6,}  {pct:4.1f}%  {mb:8.1f}  @{domain}")

    print(f"\n=== TOP {top_n} DOMAINS BY STORAGE ===")
    print(f"  {'MB':>8}  {'PCT':>5}  {'COUNT':>6}  {'DOMAIN'}")
    print(f"  {'-'*8}  {'-'*5}  {'-'*6}  {'-'*40}")
    for domain, total_bytes in sorted(size_by_domain.items(), key=lambda x: x[1], reverse=True)[:top_n]:
        mb = total_bytes / 1_048_576
        pct = 100 * total_bytes / total_size
        count = count_by_domain[domain]
        print(f"  {mb:8.1f}  {pct:4.1f}%  {count:6,}  @{domain}")


def report_unsubscribe(messages, top_n):
    """Senders with List-Unsubscribe — safe targets for bulk delete + unsubscribe."""
    unsub = [m for m in messages if m["list_unsubscribe"]]
    count_by_addr = collections.Counter(m["from_addr"] for m in unsub)
    size_by_addr = collections.defaultdict(int)
    display_map = {}
    list_id_map = {}
    for m in unsub:
        addr = m["from_addr"]
        size_by_addr[addr] += m["size"]
        if addr not in display_map:
            display_map[addr] = m["from_display"]
        if addr not in list_id_map and m["list_id"]:
            list_id_map[addr] = m["list_id"]

    total_unsub = len(unsub)
    total_size = sum(size_by_addr.values())

    print(f"\n=== UNSUBSCRIBE CANDIDATES — TOP {top_n} BY MESSAGE COUNT ===")
    print(f"  These senders include a List-Unsubscribe header — they are mailing lists")
    print(f"  or marketing mail. Safe to bulk-delete and unsubscribe from.")
    print(f"  In Gmail: search  from:SENDER  → Select All → Delete.")
    print(f"  ({total_unsub:,} messages have List-Unsubscribe  /  {total_size/1_048_576:.0f} MB total)")
    print(f"  {'COUNT':>6}  {'PCT':>5}  {'MB':>8}  {'SENDER / LIST-ID'}")
    print(f"  {'-'*6}  {'-'*5}  {'-'*8}  {'-'*60}")
    for addr, count in count_by_addr.most_common(top_n):
        pct = 100 * count / total_unsub if total_unsub else 0
        mb = size_by_addr[addr] / 1_048_576
        lid = f"  [{list_id_map[addr]}]" if addr in list_id_map else ""
        print(f"  {count:6,}  {pct:4.1f}%  {mb:8.1f}  {display_map[addr]}{lid}")

    print(f"\n=== UNSUBSCRIBE CANDIDATES — TOP {top_n} BY STORAGE ===")
    print(f"  {'MB':>8}  {'PCT':>5}  {'COUNT':>6}  {'SENDER / LIST-ID'}")
    print(f"  {'-'*8}  {'-'*5}  {'-'*6}  {'-'*60}")
    for addr, total_bytes in sorted(size_by_addr.items(), key=lambda x: x[1], reverse=True)[:top_n]:
        mb = total_bytes / 1_048_576
        pct = 100 * total_bytes / total_size if total_size else 0
        count = count_by_addr[addr]
        lid = f"  [{list_id_map[addr]}]" if addr in list_id_map else ""
        print(f"  {mb:8.1f}  {pct:4.1f}%  {count:6,}  {display_map[addr]}{lid}")


def report_attachments(messages, top_n):
    """Top senders by attachment storage."""
    att_msgs = [m for m in messages if m["has_attachment"]]
    size_by_addr = collections.defaultdict(int)
    count_by_addr = collections.Counter()
    display_map = {}
    for m in att_msgs:
        addr = m["from_addr"]
        size_by_addr[addr] += m["attachment_size"]
        count_by_addr[addr] += 1
        if addr not in display_map:
            display_map[addr] = m["from_display"]

    total_att_size = sum(size_by_addr.values())
    total_att_msgs = len(att_msgs)

    print(f"\n=== TOP {top_n} SENDERS BY ATTACHMENT STORAGE ===")
    print(f"  Attachments are the biggest storage culprits. Consider downloading")
    print(f"  important attachments locally, then deleting these threads in Gmail.")
    print(f"  ({total_att_msgs:,} messages with attachments  /  {total_att_size/1_048_576:.0f} MB total attachment data)")
    print(f"  {'MB':>8}  {'PCT':>5}  {'COUNT':>6}  {'SENDER'}")
    print(f"  {'-'*8}  {'-'*5}  {'-'*6}  {'-'*50}")
    for addr, total_bytes in sorted(size_by_addr.items(), key=lambda x: x[1], reverse=True)[:top_n]:
        mb = total_bytes / 1_048_576
        pct = 100 * total_bytes / total_att_size if total_att_size else 0
        count = count_by_addr[addr]
        print(f"  {mb:8.1f}  {pct:4.1f}%  {count:6,}  {display_map[addr]}")


def report_age_size(messages, top_n, min_age_years, min_size_kb):
    """Old and large messages — likely forgotten storage hogs."""
    now = datetime.now(timezone.utc)
    min_size_bytes = min_size_kb * 1024
    cutoff_year = now.year - min_age_years

    candidates = [
        m for m in messages
        if m["size"] >= min_size_bytes
        and m["date"] is not None
        and m["date"].year <= cutoff_year
    ]
    candidates.sort(key=lambda m: m["size"], reverse=True)

    total_size = sum(m["size"] for m in candidates)
    print(f"\n=== OLD + LARGE MESSAGES (older than {min_age_years}y, larger than {min_size_kb} KB) ===")
    print(f"  Forgotten large messages. In Gmail: open the message, verify you no")
    print(f"  longer need it (or save attachments locally), then delete.")
    print(f"  Tip: search  older_than:{min_age_years}y larger:{min_size_kb}k  in Gmail to find these.")
    print(f"  ({len(candidates):,} messages  /  {total_size/1_048_576:.0f} MB total)")
    print(f"  {'KB':>8}  {'DATE'}        {'ATT':>3}  {'SENDER':<35}  {'SUBJECT'}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*3}  {'-'*35}  {'-'*40}")
    for m in candidates[:top_n]:
        kb = m["size"] / 1024
        date_str = m["date"].strftime("%Y-%m-%d")
        att = "yes" if m["has_attachment"] else "no "
        print(f"  {kb:8.0f}  {date_str}  {att}  {m['from_display'][:35]:<35}  {m['subject'][:40]}")


def report_threads(messages, top_n):
    """Top email threads by total storage."""
    size_by_thread = collections.defaultdict(int)
    count_by_thread = collections.Counter()
    subject_by_thread = {}
    senders_by_thread = collections.defaultdict(set)

    for m in messages:
        tid = m["thread_id"]
        if not tid:
            continue
        size_by_thread[tid] += m["size"]
        count_by_thread[tid] += 1
        if tid not in subject_by_thread:
            subj = re.sub(r"^(re|fwd?|fw):\s*", "", m["subject"], flags=re.I).strip()
            subject_by_thread[tid] = subj
        senders_by_thread[tid].add(m["from_addr"])

    print(f"\n=== TOP {top_n} THREADS BY STORAGE ===")
    print(f"  Large threads often contain many embedded images or attachment chains.")
    print(f"  In Gmail: search the subject line, select the thread, delete it.")
    print(f"  {'MB':>7}  {'MSGS':>4}  {'PARTICIPANTS':>12}  {'SUBJECT'}")
    print(f"  {'-'*7}  {'-'*4}  {'-'*12}  {'-'*50}")
    for tid, total_bytes in sorted(size_by_thread.items(), key=lambda x: x[1], reverse=True)[:top_n]:
        mb = total_bytes / 1_048_576
        count = count_by_thread[tid]
        participants = len(senders_by_thread[tid])
        subject = subject_by_thread.get(tid, "(unknown)")[:50]
        print(f"  {mb:7.1f}  {count:4,}  {participants:12,}  {subject}")


def report_never_replied(messages, top_n, my_email=None):
    """Senders you have never written back to."""
    if not my_email:
        # Auto-detect: the most common To: address in the dataset is almost certainly the user
        to_counter = collections.Counter(m["to_addr"] for m in messages if m["to_addr"])
        if not to_counter:
            print("\n=== NEVER-REPLIED SENDERS ===")
            print("  Cannot auto-detect your email address. Use --my-email to specify it.")
            return
        my_email = to_counter.most_common(1)[0][0]
        print(f"  (Auto-detected your address as: {my_email})", file=sys.stderr)

    sent = [m for m in messages if m["from_addr"] == my_email]
    replied_to = {m["to_addr"] for m in sent if m["to_addr"]}

    received = [m for m in messages if m["from_addr"] != my_email]
    count_by_addr = collections.Counter(m["from_addr"] for m in received)
    size_by_addr = collections.defaultdict(int)
    display_map = {}
    for m in received:
        addr = m["from_addr"]
        size_by_addr[addr] += m["size"]
        if addr not in display_map:
            display_map[addr] = m["from_display"]

    never = {addr: count for addr, count in count_by_addr.items() if addr not in replied_to}
    total_never = sum(never.values())
    total_size = sum(size_by_addr[a] for a in never)

    print(f"\n=== TOP {top_n} NEVER-REPLIED SENDERS BY MESSAGE COUNT ===")
    print(f"  You have never written back to these senders. Likely notifications,")
    print(f"  newsletters, or alerts you could delete or set up a Gmail filter to")
    print(f"  skip the inbox (Label: skip inbox, mark as read).")
    print(f"  ({len(never):,} senders  /  {total_never:,} messages  /  {total_size/1_048_576:.0f} MB)")
    print(f"  {'COUNT':>6}  {'MB':>8}  {'SENDER'}")
    print(f"  {'-'*6}  {'-'*8}  {'-'*50}")
    for addr, count in sorted(never.items(), key=lambda x: x[1], reverse=True)[:top_n]:
        mb = size_by_addr[addr] / 1_048_576
        print(f"  {count:6,}  {mb:8.1f}  {display_map[addr]}")


def report_timeline(messages):
    dated = [m for m in messages if m["date"]]
    by_month = collections.Counter(m["date"].strftime("%Y-%m") for m in dated)
    print("\n=== MESSAGES PER MONTH ===")
    for month in sorted(by_month):
        bar = "#" * (by_month[month] // 5)
        print(f"  {month}  {by_month[month]:5,}  {bar}")


def report_largest(messages, top_n):
    sorted_msgs = sorted(messages, key=lambda m: m["size"], reverse=True)
    print(f"\n=== TOP {top_n} LARGEST MESSAGES ===")
    print(f"  {'KB':>8}  {'DATE'}        {'ATT':>3}  {'SENDER':<35}  {'SUBJECT'}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*3}  {'-'*35}  {'-'*40}")
    for m in sorted_msgs[:top_n]:
        kb = m["size"] / 1024
        date_str = m["date"].strftime("%Y-%m-%d") if m["date"] else "unknown   "
        att = "yes" if m["has_attachment"] else "no "
        print(f"  {kb:8.0f}  {date_str}  {att}  {m['from_display'][:35]:<35}  {m['subject'][:40]}")


def report_subjects(messages, top_n):
    word_counter = collections.Counter()
    for m in messages:
        word_counter.update(subject_words(m["subject"]))
    print(f"\n=== TOP {top_n} SUBJECT WORDS ===")
    for word, count in word_counter.most_common(top_n):
        print(f"  {count:5,}  {word}")


def report_default(messages, top_n, my_email, min_age_years, min_size_kb):
    """Curated inbox-reduction report."""
    total_size = sum(m["size"] for m in messages)
    dated = [m for m in messages if m["date"]]
    oldest = min(m["date"] for m in dated) if dated else None
    newest = max(m["date"] for m in dated) if dated else None

    print("=" * 70)
    print("  INBOX REDUCTION REPORT")
    if oldest:
        print(f"  {len(messages):,} messages  •  {total_size/1_048_576:.0f} MB  •  {oldest.strftime('%Y-%m-%d')} – {newest.strftime('%Y-%m-%d')}")
    print("=" * 70)

    report_unsubscribe(messages, top_n)
    report_attachments(messages, top_n)

    size_by_addr = collections.defaultdict(int)
    display_map = {}
    for m in messages:
        size_by_addr[m["from_addr"]] += m["size"]
        if m["from_addr"] not in display_map:
            display_map[m["from_addr"]] = m["from_display"]
    total_size_all = sum(size_by_addr.values())
    print(f"\n=== TOP {top_n} SENDERS BY STORAGE ===")
    print(f"  {'MB':>8}  {'PCT':>5}  {'COUNT':>6}  {'SENDER'}")
    print(f"  {'-'*8}  {'-'*5}  {'-'*6}  {'-'*50}")
    count_by_addr = collections.Counter(m["from_addr"] for m in messages)
    for addr, total_bytes in sorted(size_by_addr.items(), key=lambda x: x[1], reverse=True)[:top_n]:
        mb = total_bytes / 1_048_576
        pct = 100 * total_bytes / total_size_all
        print(f"  {mb:8.1f}  {pct:4.1f}%  {count_by_addr[addr]:6,}  {display_map[addr]}")

    report_threads(messages, top_n)
    report_age_size(messages, top_n, min_age_years, min_size_kb)
    report_never_replied(messages, top_n, my_email)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze Thunderbird All Mail for Gmail inbox reduction.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mbox", default=str(ALL_MAIL) if ALL_MAIL else None,
        help="Path to the mbox file (default: auto-detected All Mail)",
    )
    parser.add_argument(
        "--report", default="default",
        choices=["default", "summary", "senders", "unsubscribe", "attachments",
                 "age-size", "threads", "never-replied", "timeline", "subjects",
                 "largest", "all"],
        help="Which report to run (default: default)",
    )
    parser.add_argument("--top", type=int, default=20, help="Number of results per table (default: 20)")
    parser.add_argument("--year", type=int, default=None, help="Filter to a specific year")
    parser.add_argument("--generate-cache", action="store_true", help="Force rebuild the cache")
    parser.add_argument(
        "--older-than", default="24 hours", metavar="DURATION",
        help="Regenerate cache if older than this (default: '24 hours')",
    )
    parser.add_argument(
        "--my-email", default=None, metavar="EMAIL",
        help="Your email address, for the never-replied report (auto-detected if omitted)",
    )
    parser.add_argument(
        "--min-age", type=int, default=2, metavar="YEARS",
        help="Minimum age in years for the age-size report (default: 2)",
    )
    parser.add_argument(
        "--min-size", type=int, default=500, metavar="KB",
        help="Minimum size in KB for the age-size report (default: 500)",
    )
    args = parser.parse_args()
    older_than_secs = parse_duration(args.older_than)

    if not args.mbox:
        parser.error("Could not auto-detect Thunderbird profile. Use --mbox to specify the mbox file.")

    messages = load_messages(
        args.mbox, year=args.year,
        generate_cache=args.generate_cache,
        older_than=older_than_secs,
    )
    if not messages:
        print("No messages found.")
        return

    r = args.report
    if r in ("default",):
        report_default(messages, args.top, args.my_email, args.min_age, args.min_size)
    if r in ("summary", "all"):
        report_summary(messages, args.top)
    if r in ("senders", "all"):
        report_senders(messages, args.top)
    if r in ("unsubscribe", "all"):
        report_unsubscribe(messages, args.top)
    if r in ("attachments", "all"):
        report_attachments(messages, args.top)
    if r in ("age-size", "all"):
        report_age_size(messages, args.top, args.min_age, args.min_size)
    if r in ("threads", "all"):
        report_threads(messages, args.top)
    if r in ("never-replied", "all"):
        report_never_replied(messages, args.top, args.my_email)
    if r in ("timeline", "all"):
        report_timeline(messages)
    if r in ("subjects", "all"):
        report_subjects(messages, args.top)
    if r in ("largest", "all"):
        report_largest(messages, args.top)


if __name__ == "__main__":
    main()
