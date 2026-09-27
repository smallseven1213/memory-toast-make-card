#!/usr/bin/env python3
"""Publish a Memory Toast deck to the public Library, or release a new version.

Reads the deck's .memory-toast.json (written by upload_pack.py) for deckId and
libraryPackId, so you just point it at the same deck directory. Auth uses the
refresh token stored by mt_login.py — no password. Zero deps (Python 3.9+ stdlib).

CLI:
  library_pack.py publish DECK_DIR --description TEXT --category CAT
                  [--title T] [--language zh-TW] [--learning-language es]
                  [--tags a,b,c] [--api URL]
  library_pack.py release DECK_DIR [--changelog TEXT] [--api URL]
  library_pack.py preview DECK_DIR [--cards 3,7,12,…] [--title-suffix 試讀本] [--api URL]
  library_pack.py status  [--api URL]

Categories (key): language science history programming math geography exam other

publish  → makes the deck's current pack public as a NEW library pack (once).
release  → publishes the deck's CURRENT pack as a NEW library version. Push the
           new deck content with upload_pack.py FIRST, then release.
preview  → builds a free ≤10-card 試讀本 (preview pack) of an already-published
           deck: slices the deck into a sibling <slug>-preview dir, uploads it as
           its own deck, and publishes it as a library pack linked to the full
           pack (previewOfLibraryPackId). Re-running pushes a new version.
status   → lists your published library packs.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from _mt_auth import api_call, fail, get_access_token, load_credentials, resolve_api_url, premium_gate
from upload_pack import DECK_RECORD, read_record, write_record

CATEGORIES = ["language", "science", "history", "programming", "math", "geography", "exam", "other"]

# Audience / explanation language a pack declares — the language its card backs are
# written in (who the deck is FOR), NOT what it teaches (that's --learning-language).
# Aligned with the site's UI locales (validators/library.ts EXPLANATION_LANGUAGES).
# Off-list or missing -> "en".
EXPLANATION_LANGUAGES = ["en", "zh-TW", "zh-Hans", "ja", "es", "vi"]


def norm_language(value):
    return value if value in EXPLANATION_LANGUAGES else "en"
PREVIEW_MAX_CARDS = 10        # a 試讀本 is hard-capped at 10 cards
PREVIEW_MIN_FULL_CARDS = 50   # the full deck must have MORE than this to make a preview
SCRIPTS_DIR = Path(__file__).resolve().parent


def _deck_id_from_record(deck_dir: Path):
    record = read_record(deck_dir)
    deck_id = record.get("deckId")
    if not deck_id:
        fail(f"no deckId in {deck_dir / DECK_RECORD} — run upload_pack.py first to upload the deck")
    return deck_id, record


def cmd_publish(args) -> None:
    deck_dir = args.deck_dir.resolve()
    deck_id, record = _deck_id_from_record(deck_dir)
    api = resolve_api_url(args.api)
    token = get_access_token(api)

    body = {
        "deckId": deck_id,
        "title": (args.title or record.get("title") or "")[:100],
        "description": args.description,
        "category": args.category,
        "language": norm_language(args.language or record.get("language")),
    }
    if args.learning_language:
        body["learningLanguage"] = args.learning_language
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()][:10]

    status, res = api_call("POST", f"{api}/api/v1/library/publish", body, token)
    premium_gate(status, res)
    if status == 409:
        lp_id = (res.get("libraryPack") or {}).get("id")
        if lp_id:
            write_record(deck_dir, {"libraryPackId": lp_id})
        fail(f"already published as libraryPack {lp_id} — use `release` to push a new version.")
    if status != 201:
        fail(f"publish failed ({status}): {res}")
    lp = res["libraryPack"]
    write_record(deck_dir, {"libraryPackId": lp["id"]})
    print(f"Published. libraryPack={lp['id']} category={args.category}")
    print(f"Recorded libraryPackId in {DECK_RECORD}. The deck is now public in the Library.")


def cmd_release(args) -> None:
    deck_dir = args.deck_dir.resolve()
    deck_id, record = _deck_id_from_record(deck_dir)
    lp_id = record.get("libraryPackId")
    if not lp_id:
        fail(f"no libraryPackId in {DECK_RECORD} — run `library_pack.py publish` first")
    api = resolve_api_url(args.api)
    token = get_access_token(api)

    body = {"deckId": deck_id}
    if args.changelog:
        body["changelog"] = args.changelog
    status, res = api_call("POST", f"{api}/api/v1/library/packs/{lp_id}/release", body, token)
    premium_gate(status, res)
    if status != 201:
        fail(f"release failed ({status}): {res}")
    ver = (res.get("pack") or {}).get("version")
    print(f"Released new library version v{ver} for libraryPack={lp_id}.")


# ---------------------------------------------------------------------------
# 試讀本 (preview pack) — slicing + media copy. These two functions are kept
# byte-identical between the internal (.env) and public (token) skill copies so
# the preview produced is the same regardless of which copy ran. The auth/upload
# wiring around them (cmd_preview) is what differs per copy.
# ---------------------------------------------------------------------------

def _parse_cards_arg(raw: str, cards: list) -> list:
    """Resolve a --cards token list into 0-based indices into `cards`.

    Each comma-separated token is matched, in order:
      1. against a card's `id` field (exact string match), else
      2. as a 1-based index into the deck's card array.
    Duplicates are dropped (first occurrence wins) while preserving order.
    Errors on an unknown id, a non-numeric token, or an out-of-range index.
    """
    by_id = {}
    for i, c in enumerate(cards):
        cid = c.get("id")
        if isinstance(cid, str) and cid:
            by_id.setdefault(cid, i)
    picked, seen = [], set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok in by_id:
            idx = by_id[tok]
        else:
            try:
                n = int(tok)
            except ValueError:
                fail(f"--cards: {tok!r} is not a card id and not a number")
            if n < 1 or n > len(cards):
                fail(f"--cards: index {n} out of range (deck has {len(cards)} cards, 1-based)")
            idx = n - 1
        if idx not in seen:
            seen.add(idx)
            picked.append(idx)
    if not picked:
        fail("--cards parsed to an empty selection")
    return picked


def select_preview_cards(deck: dict, cards_arg) -> list:
    """Return the slice of cards for the preview: default first 10, or the
    explicit --cards selection. Always capped at PREVIEW_MAX_CARDS."""
    cards = deck.get("cards") or []
    if cards_arg:
        idxs = _parse_cards_arg(cards_arg, cards)
        if len(idxs) > PREVIEW_MAX_CARDS:
            fail(f"--cards lists {len(idxs)} cards — a 試讀本 is capped at {PREVIEW_MAX_CARDS}")
        idxs = idxs[:PREVIEW_MAX_CARDS]
        return [cards[i] for i in idxs]
    return cards[:PREVIEW_MAX_CARDS]


def _iter_card_media_refs(card: dict):
    """Yield every local-file relative path referenced by one card, walking both
    the legacy sections (front/backSections) and the ordered blocks
    (front/backBlocks: image `file` + every text block's audios[] `file`)."""
    for skey in ("frontSections", "backSections"):
        for sec in card.get(skey) or []:
            if isinstance(sec, dict) and sec.get("file"):
                yield sec["file"]
    for bkey in ("frontBlocks", "backBlocks"):
        for blk in card.get(bkey) or []:
            if not isinstance(blk, dict):
                continue
            if blk.get("file"):
                yield blk["file"]
            for a in blk.get("audios") or []:
                if isinstance(a, dict) and a.get("file"):
                    yield a["file"]


def build_preview_dir(full_dir: Path, preview_dir: Path, selected: list,
                      full_deck: dict, title_suffix: str) -> dict:
    """Write the sibling preview deck dir: a deck.json with the selected cards and
    only their referenced media copied from the full deck's tree (preserving the
    relative paths). Returns a small summary dict. Idempotent — clears any prior
    preview build first so a re-run reflects the new selection exactly."""
    if preview_dir.exists():
        # Remove everything EXCEPT the AI record (.memory-toast.json), so the
        # preview's deckId/libraryPackId survive a re-run (-> release, not a dup).
        for child in preview_dir.iterdir():
            if child.name == DECK_RECORD:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    preview_dir.mkdir(parents=True, exist_ok=True)

    base_title = (full_deck.get("title") or "").strip()
    preview_deck = {
        "title": f"{base_title} — {title_suffix}"[:200],
        "description": full_deck.get("description", ""),
        "language": norm_language(full_deck.get("language")),
        "tags": full_deck.get("tags", []),
        "cards": selected,
    }
    (preview_dir / "deck.json").write_text(
        json.dumps(preview_deck, ensure_ascii=False, indent=2) + "\n")

    copied, missing = 0, []
    for card in selected:
        for rel in _iter_card_media_refs(card):
            src = (full_dir / rel).resolve()
            dst = (preview_dir / rel).resolve()
            if not src.is_file():
                missing.append(rel)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)
                copied += 1
    if missing:
        fail("preview build: referenced media not found under the full deck dir:\n  - "
             + "\n  - ".join(sorted(set(missing))))
    return {"cards": len(selected), "media_copied": copied}


def _preview_dir_for(full_dir: Path) -> Path:
    """Sibling preview dir: <full-dir>-preview, beside the full deck dir."""
    return full_dir.with_name(full_dir.name + "-preview")


def cmd_preview(args) -> None:
    full_dir = args.deck_dir.resolve()
    if not full_dir.is_dir():
        fail(f"deck dir not found: {full_dir}")
    full_record = read_record(full_dir)
    full_lp_id = full_record.get("libraryPackId")
    if not full_lp_id:
        fail("the full deck has no libraryPackId — publish the full pack first "
             f"(`library_pack.py publish {full_dir.name} …`)")

    deck_json_path = full_dir / "deck.json"
    if not deck_json_path.is_file():
        fail(f"missing {deck_json_path}")
    try:
        full_deck = json.loads(deck_json_path.read_text())
    except json.JSONDecodeError as e:
        fail(f"deck.json is not valid JSON: {e}")
    full_cards = full_deck.get("cards")
    if not isinstance(full_cards, list) or not full_cards:
        fail("deck.json must contain a non-empty 'cards' array")
    if len(full_cards) <= PREVIEW_MIN_FULL_CARDS:
        fail("卡包需超過 50 張才能製作試讀本")

    selected = select_preview_cards(full_deck, args.cards)
    preview_dir = _preview_dir_for(full_dir)
    summary = build_preview_dir(full_dir, preview_dir, selected, full_deck, args.title_suffix)
    print(f"Built preview deck at {preview_dir}")
    print(f"  cards: {summary['cards']}  media copied: {summary['media_copied']}")

    # Upload the preview as its own deck (reuse upload_pack.py end-to-end). This
    # creates/updates the preview's deckId + writes the preview's .memory-toast.json.
    upload_cmd = [sys.executable, str(SCRIPTS_DIR / "upload_pack.py"), str(preview_dir)]
    if args.api:
        upload_cmd += ["--api", args.api]
    print(f"Uploading preview deck → {' '.join(upload_cmd)}")
    res = subprocess.run(upload_cmd)
    if res.returncode != 0:
        fail("preview upload (upload_pack.py) failed — see output above")

    preview_record = read_record(preview_dir)
    preview_deck_id = preview_record.get("deckId")
    if not preview_deck_id:
        fail(f"upload_pack.py did not record a deckId in {preview_dir / DECK_RECORD}")

    api = resolve_api_url(args.api)
    token = get_access_token(api)

    preview_lp_id = preview_record.get("libraryPackId")
    if preview_lp_id:
        # Already a library pack → release a new version instead of re-publishing.
        status, rel = api_call(
            "POST", f"{api}/api/v1/library/packs/{preview_lp_id}/release",
            {"deckId": preview_deck_id, "changelog": "Refresh preview"}, token)
        if status != 201:
            fail(f"preview release failed ({status}): {rel}")
        ver = (rel.get("pack") or {}).get("version")
        print(f"Released preview library version v{ver} for libraryPack={preview_lp_id}.")
    else:
        body = {
            "deckId": preview_deck_id,
            "title": (f"{(full_deck.get('title') or '').strip()} — {args.title_suffix}")[:100],
            "description": full_deck.get("description") or full_deck.get("title") or "Preview",
            "category": args.category or full_record.get("category") or "language",
            "language": norm_language(args.language or full_deck.get("language")),
            "priceTokens": 0,
            "previewOfLibraryPackId": full_lp_id,
        }
        learning = args.learning_language or full_record.get("learningLanguage")
        if learning:
            body["learningLanguage"] = learning
        status, pub = api_call("POST", f"{api}/api/v1/library/publish", body, token)
        premium_gate(status, pub)
        if status == 409:
            lp_id = (pub.get("libraryPack") or {}).get("id")
            if lp_id:
                preview_lp_id = lp_id
                write_record(preview_dir, {"libraryPackId": lp_id})
            else:
                fail(f"preview publish conflict, no id returned: {pub}")
        elif status != 201:
            fail(f"preview publish failed ({status}): {pub}")
        else:
            preview_lp_id = pub["libraryPack"]["id"]
            print(f"Published preview. libraryPack={preview_lp_id} (free, previewOf={full_lp_id})")
        write_record(preview_dir, {"libraryPackId": preview_lp_id})

    # Cross-link both records.
    write_record(preview_dir, {"previewOfLibraryPackId": full_lp_id})
    write_record(full_dir, {
        "previewDeckId": preview_deck_id,
        "previewLibraryPackId": preview_lp_id,
    })
    print(f"Linked: full pack {full_lp_id} ↔ preview pack {preview_lp_id}")
    print(f"Recorded previewDeckId / previewLibraryPackId in {full_dir / DECK_RECORD}.")


def cmd_status(args) -> None:
    api = resolve_api_url(args.api)
    token = get_access_token(api)
    status, res = api_call("GET", f"{api}/api/v1/library/my-published", token=token)
    if status != 200:
        fail(f"status failed ({status}): {res}")
    packs = res.get("packs", [])
    who = load_credentials().get("email", "you")
    print(f"{who} has {len(packs)} published library pack(s):")
    for p in packs:
        ver = p.get("latestVersion") or p.get("version")
        print(f"  - {p.get('title')}  id={p.get('id')}  category={p.get('category')}  v{ver}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api", help="override API base URL")

    pub = sub.add_parser("publish", parents=[common], help="publish the deck to the Library (once)")
    pub.add_argument("deck_dir", type=Path)
    pub.add_argument("--description", required=True, help="1-500 chars, shown in the Library")
    pub.add_argument("--category", required=True, choices=CATEGORIES)
    pub.add_argument("--title", help="default: title from .memory-toast.json (max 100)")
    pub.add_argument("--language", choices=EXPLANATION_LANGUAGES,
                     help="audience/explanation language the card backs are written in "
                          "(default: deck.json language, else en)")
    pub.add_argument("--learning-language", help="language being learned, e.g. es / ja")
    pub.add_argument("--tags", help="comma-separated, max 10")
    pub.set_defaults(func=cmd_publish)

    rel = sub.add_parser("release", parents=[common], help="release a new version of a published deck")
    rel.add_argument("deck_dir", type=Path)
    rel.add_argument("--changelog", help="1-500 chars describing what changed")
    rel.set_defaults(func=cmd_release)

    prev = sub.add_parser(
        "preview", parents=[common],
        help="build + publish a free ≤10-card 試讀本 (preview) of a published deck")
    prev.add_argument("deck_dir", type=Path, help="the FULL (already-published) deck dir")
    prev.add_argument("--cards",
                      help="comma-separated 1-based indices and/or card ids to include "
                           f"(default: first {PREVIEW_MAX_CARDS}; max {PREVIEW_MAX_CARDS})")
    prev.add_argument("--title-suffix", default="試讀本",
                      help='appended after " — " to the full title (default: 試讀本)')
    prev.add_argument("--category", choices=CATEGORIES,
                      help="library category for the preview (default: full deck's, else language)")
    prev.add_argument("--language", choices=EXPLANATION_LANGUAGES,
                     help="audience/explanation language (default: full deck's language, else en)")
    prev.add_argument("--learning-language", help="language being learned, e.g. es / ja")
    prev.set_defaults(func=cmd_preview)

    st = sub.add_parser("status", parents=[common], help="list your published library packs")
    st.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
