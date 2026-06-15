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
  library_pack.py status  [--api URL]

Categories (key): language science history programming math geography exam other

publish  → makes the deck's current pack public as a NEW library pack (once).
release  → publishes the deck's CURRENT pack as a NEW library version. Push the
           new deck content with upload_pack.py FIRST, then release.
status   → lists your published library packs.
"""

import argparse
from pathlib import Path

from _mt_auth import api_call, fail, get_access_token, load_credentials, resolve_api_url
from upload_pack import DECK_RECORD, read_record, write_record

CATEGORIES = ["language", "science", "history", "programming", "math", "geography", "exam", "other"]


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
        "language": args.language or record.get("language") or "zh-TW",
    }
    if args.learning_language:
        body["learningLanguage"] = args.learning_language
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()][:10]

    status, res = api_call("POST", f"{api}/api/v1/library/publish", body, token)
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
    if status != 201:
        fail(f"release failed ({status}): {res}")
    ver = (res.get("pack") or {}).get("version")
    print(f"Released new library version v{ver} for libraryPack={lp_id}.")


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
    pub.add_argument("--language", help="content language (default: deck language)")
    pub.add_argument("--learning-language", help="language being learned, e.g. es / ja")
    pub.add_argument("--tags", help="comma-separated, max 10")
    pub.set_defaults(func=cmd_publish)

    rel = sub.add_parser("release", parents=[common], help="release a new version of a published deck")
    rel.add_argument("deck_dir", type=Path)
    rel.add_argument("--changelog", help="1-500 chars describing what changed")
    rel.set_defaults(func=cmd_release)

    st = sub.add_parser("status", parents=[common], help="list your published library packs")
    st.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
