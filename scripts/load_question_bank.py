"""
load_question_bank.py — Load the Daggerheart Question Bank (Master) into
content.questions (Content-Ops Phase 1).

Parses the vault master doc's `## Full bank by category` section: each
`### <Category> (N)` header starts a 5-column pipe table
(Question | Sub-topic | Rec. | Tie | Source). Rows are upserted on
question_hash = sha256(normalized question text), so re-runs and future
F5Bot/sweep appends are idempotent.

Idempotent: ON CONFLICT (question_hash) refreshes last_seen_at + mutable fields.

Drift guard: asserts exactly 5 columns per table and the known 16-category set;
fails loud on mismatch.

Usage:
  python scripts/load_question_bank.py                 # default doc + batch
  python scripts/load_question_bank.py --dry-run
  python scripts/load_question_bank.py --file /path/to/doc.md --batch master-2026-06-23
"""

import argparse
import hashlib
import os
import re
import unicodedata
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DB_CONFIG = {
    "host":     os.getenv("BUSINESS_DB_HOST", "10.0.0.16"),
    "port":     int(os.getenv("BUSINESS_DB_PORT", 5433)),
    "dbname":   os.getenv("BUSINESS_DB_NAME", "business"),
    "user":     os.getenv("BUSINESS_DB_USER"),
    "password": os.getenv("BUSINESS_DB_PASSWORD"),
}

DEFAULT_DOC = Path(
    "/Users/bnieco/Library/Mobile Documents/iCloud~md~obsidian/Documents"
    "/Obsidian Vault/Projects/Etsy Business"
    "/Daggerheart Question Bank (Master).md"
)

FULL_BANK_HEADER = "## Full bank by category"

# Known 16-category set (matches the doc's "Questions by category" table).
KNOWN_CATEGORIES = {
    "GM: Adversaries, Environments & Encounter Balancing",
    "Coming from D&D / Onboarding",
    "Combat & Action Economy (spotlight / tag-team)",
    "Character Creation (ancestry / community / class choice / build order)",
    "Classes, Subclasses & Domains",
    "Hope & Fear Economy (+ Experiences)",
    "Equipment, Gold, Loot & Crafting",
    "Magic & Spellcasting",
    "Campaign Frames & Session Zero",
    "Stress & Conditions",
    "Homebrew, Adjudication & Misc",
    "The Duality Dice & Core Rolls",
    "Leveling, Advancement & Multiclassing",
    "Damage, HP, Thresholds & Armor",
    "Rest, Downtime & Projects",
    "Death & Dying",
}

RECURRENCE_MAP = {
    "comm": "common", "common": "common",
    "occ": "occasional", "occa": "occasional", "occasional": "occasional",
    "rare": "rare",
}
TIE_MAP = {
    "y": "Y", "yes": "Y",
    "maybe": "maybe",
    "n": "N", "no": "N",
}

LINK_RE = re.compile(r"\[[^\]]*\]\((?P<url>[^)]+)\)")
HEADER_RE = re.compile(r"^###\s+(?P<cat>.+?)\s*\((?P<n>\d+)\)\s*$")
SEP_RE = re.compile(r"^\|?\s*:?-{2,}.*$")  # table separator like |---|---|...


def unwrap_link(cell: str) -> str | None:
    """Pull the URL out of a [label](url) markdown link; else return raw text."""
    cell = cell.strip()
    if not cell:
        return None
    m = LINK_RE.search(cell)
    if m:
        return m.group("url").strip()
    return cell or None


def normalize_question(text_in: str) -> str:
    """Normalize for hashing: fold smart quotes, lowercase, collapse whitespace,
    strip trailing punctuation."""
    s = unicodedata.normalize("NFKC", text_in)
    # Fold smart quotes / dashes to ASCII equivalents
    for a, b in (
        ("‘", "'"), ("’", "'"), ("‛", "'"),
        ("“", '"'), ("”", '"'),
        ("–", "-"), ("—", "-"), ("‒", "-"),
        ("…", "..."),
    ):
        s = s.replace(a, b)
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(" .?!,;:…")
    return s


def question_hash(text_in: str) -> str:
    return hashlib.sha256(normalize_question(text_in).encode("utf-8")).hexdigest()


def split_row(line: str) -> list[str]:
    """Split a markdown table row into cells (strip the leading/trailing pipes)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_doc(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()

    # Locate the Full bank section.
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == FULL_BANK_HEADER:
            start = i + 1
            break
    if start is None:
        raise SystemExit(f"FATAL: '{FULL_BANK_HEADER}' not found in {path}")

    records: list[dict] = []
    seen_categories: dict[str, int] = {}
    current_cat: str | None = None
    declared_n: int | None = None
    in_table = False
    cat_count = 0

    def close_category():
        nonlocal current_cat, declared_n, cat_count
        if current_cat is not None:
            seen_categories[current_cat] = cat_count
            if declared_n is not None and cat_count != declared_n:
                print(
                    f"  NOTE: '{current_cat}' parsed {cat_count} rows "
                    f"(header declared {declared_n})"
                )

    for ln in lines[start:]:
        stripped = ln.strip()

        # A new top-level (##) section ends the full bank.
        if stripped.startswith("## ") and not stripped.startswith("### "):
            break

        hm = HEADER_RE.match(stripped)
        if hm:
            close_category()
            current_cat = hm.group("cat").strip()
            declared_n = int(hm.group("n"))
            cat_count = 0
            in_table = False
            if current_cat not in KNOWN_CATEGORIES:
                raise SystemExit(
                    f"FATAL drift guard: unknown category '{current_cat}'. "
                    f"Known set has {len(KNOWN_CATEGORIES)} categories."
                )
            continue

        if not stripped.startswith("|"):
            continue
        if SEP_RE.match(stripped):
            continue

        cells = split_row(stripped)

        # Header row of the table (Question | Sub-topic | Rec. | Tie | Source)
        if cells and cells[0].lower() == "question":
            if len(cells) != 5:
                raise SystemExit(
                    f"FATAL drift guard: table under '{current_cat}' has "
                    f"{len(cells)} header columns, expected 5: {cells}"
                )
            in_table = True
            continue

        if not in_table:
            continue

        if len(cells) != 5:
            raise SystemExit(
                f"FATAL drift guard: row under '{current_cat}' has "
                f"{len(cells)} columns, expected 5: {cells}"
            )

        q_text, sub_topic, rec_raw, tie_raw, source_raw = cells
        q_text = q_text.strip()
        if not q_text:
            continue

        rec_key = rec_raw.strip().lower()
        if rec_key not in RECURRENCE_MAP:
            raise SystemExit(
                f"FATAL: unrecognized recurrence '{rec_raw}' under "
                f"'{current_cat}' for question: {q_text[:60]}"
            )
        tie_key = tie_raw.strip().lower()
        if tie_key not in TIE_MAP:
            raise SystemExit(
                f"FATAL: unrecognized tie value '{tie_raw}' under "
                f"'{current_cat}' for question: {q_text[:60]}"
            )

        records.append({
            "question_hash": question_hash(q_text),
            "question_text": q_text,
            "category": current_cat,
            "sub_topic": (sub_topic.strip() or None),
            "recurrence": RECURRENCE_MAP[rec_key],
            "product_tie_in": TIE_MAP[tie_key],
            "source_url": unwrap_link(source_raw),
        })
        cat_count += 1

    close_category()

    # Drift guard: exactly the known 16-category set must appear.
    missing = KNOWN_CATEGORIES - set(seen_categories)
    if missing:
        raise SystemExit(f"FATAL drift guard: categories missing from doc: {missing}")
    if len(seen_categories) != 16:
        raise SystemExit(
            f"FATAL drift guard: parsed {len(seen_categories)} categories, expected 16."
        )

    return records


def upsert(records: list[dict], batch: str, dry_run: bool = False):
    print(f"Parsed {len(records)} question rows; batch='{batch}'.")

    # Hash collisions inside the doc (paraphrase dups) — last one wins, but report.
    by_hash: dict[str, dict] = {}
    dup = 0
    for r in records:
        if r["question_hash"] in by_hash:
            dup += 1
        by_hash[r["question_hash"]] = r
    if dup:
        print(f"  NOTE: {dup} in-doc rows collapsed by question_hash "
              f"({len(by_hash)} distinct hashes).")

    if dry_run:
        print("DRY RUN — no DB writes.")
        return len(by_hash), 0, 0

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute("select count(*) from content.questions")
    before = cur.fetchone()[0]

    rows = [
        (
            r["question_hash"], r["question_text"], r["category"], r["sub_topic"],
            r["recurrence"], r["product_tie_in"], r["source_url"], batch,
        )
        for r in by_hash.values()
    ]

    psycopg2.extras.execute_values(
        cur,
        """
        insert into content.questions
            (question_hash, question_text, category, sub_topic,
             recurrence, product_tie_in, source_url, source_batch)
        values %s
        on conflict (question_hash) do update set
            last_seen_at   = now(),
            question_text  = excluded.question_text,
            category       = excluded.category,
            sub_topic      = excluded.sub_topic,
            recurrence     = excluded.recurrence,
            product_tie_in = excluded.product_tie_in,
            source_url     = excluded.source_url,
            updated_at     = now()
        """,
        rows,
        template="(%s,%s,%s,%s,%s::content.recurrence,%s::content.tie_in,%s,%s)",
    )

    conn.commit()
    cur.execute("select count(*) from content.questions")
    after = cur.fetchone()[0]
    conn.close()

    inserted = after - before
    updated = len(rows) - inserted
    print(f"Committed. content.questions: {before} -> {after} "
          f"({inserted} inserted, {updated} updated).")
    return len(rows), inserted, updated


def derive_batch(path: Path) -> str:
    """Derive 'master-YYYY-MM-DD' from the doc's frontmatter `date:` if present."""
    txt = path.read_text(encoding="utf-8")
    m = re.search(r"(?m)^date:\s*(\d{4}-\d{2}-\d{2})\s*$", txt)
    if m:
        return f"master-{m.group(1)}"
    return f"master-{date.today().isoformat()}"


def parse_args():
    p = argparse.ArgumentParser(description="Load Daggerheart Question Bank into content.questions")
    p.add_argument("--file", type=Path, default=DEFAULT_DOC)
    p.add_argument("--batch", type=str, default=None,
                   help="source_batch tag (default: derived 'master-<doc date>')")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    batch = args.batch or derive_batch(args.file)
    recs = parse_doc(args.file)
    upsert(recs, batch=batch, dry_run=args.dry_run)
