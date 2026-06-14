#!/usr/bin/env python3
"""Build a Memory Toast deck ZIP pack from a deck directory and upload it via the API.

Zero third-party deps — Python 3.9+ stdlib only.

Usage:
  upload_pack.py DECK_DIR [--dry-run] [--deck-id ID] [--local-version N] [--force]
                          [--api URL]

DECK_DIR must contain a `deck.json` (see references/pack-format.md for the schema).
Media files referenced by sections live anywhere under DECK_DIR (relative paths).

Auth: uses the refresh token stored by `mt_login.py` — no password needed. Run
`python3 scripts/mt_login.py` once before your first upload.

Modes:
  --dry-run          Build + validate the ZIP at DECK_DIR/build/pack.zip, no network.
  (default)          Create a NEW deck on the server and upload the pack.
  --deck-id ID       Upload to an EXISTING deck instead of creating one.
                     Requires --local-version N (current server pack version) or --force.
"""

import argparse
import hashlib
import json
import mimetypes
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from _mt_auth import api_call, fail, get_access_token, resolve_api_url

MAX_ZIP_BYTES = 100 * 1024 * 1024  # server-side limit in validators/sync.ts

ALLOWED_EXTS = {
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp"},
    "audio": {".mp3", ".m4a", ".wav", ".aac", ".ogg"},
    "video": {".mp4", ".mov", ".webm"},
}
EXTRA_MIME = {
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".webp": "image/webp",
}


def guess_mime(ext: str) -> str:
    if ext in EXTRA_MIME:
        return EXTRA_MIME[ext]
    mime, _ = mimetypes.guess_type(f"f{ext}")
    return mime or "application/octet-stream"


def normalize_section(sec: dict, deck_dir: Path, where: str, errors: list, media: dict) -> dict:
    """Validate one section from deck.json and convert it to the cards.json shape.

    Local files get a generated section id and a `media/{id}{ext}` storageRef;
    the file is registered in `media` (zip path -> absolute path).
    """
    kind = sec.get("kind")
    if kind not in ("image", "audio", "video"):
        errors.append(f"{where}: kind must be image|audio|video, got {kind!r}")
        return {}
    sec_id = str(uuid.uuid4())
    caption = sec.get("caption")
    has_file = bool(sec.get("file"))
    has_url = bool(sec.get("url"))
    if has_file == has_url:
        errors.append(f"{where}: exactly one of 'file' or 'url' is required")
        return {}

    if has_url:
        url = sec["url"]
        if not url.startswith(("http://", "https://")):
            errors.append(f"{where}: url must be http(s), got {url!r}")
            return {}
        storage_kind, storage_ref, mime = "external", url, None
    else:
        rel = sec["file"]
        src = (deck_dir / rel).resolve()
        if not src.is_file():
            errors.append(f"{where}: file not found: {rel}")
            return {}
        ext = src.suffix.lower()
        if ext not in ALLOWED_EXTS[kind]:
            errors.append(f"{where}: extension {ext!r} not allowed for kind {kind!r}")
            return {}
        storage_kind = "local"
        storage_ref = f"media/{sec_id}{ext}"
        mime = guess_mime(ext)
        media[storage_ref] = src

    return {
        "id": sec_id,
        "kind": kind,
        "storageKind": storage_kind,
        "storageRef": storage_ref,
        "caption": caption if caption else None,
        "mimeType": mime,
        "durationMs": sec.get("durationMs"),
    }


def build_pack(deck_dir: Path) -> dict:
    deck_json_path = deck_dir / "deck.json"
    if not deck_json_path.is_file():
        fail(f"missing {deck_json_path}")
    try:
        deck = json.loads(deck_json_path.read_text())
    except json.JSONDecodeError as e:
        fail(f"deck.json is not valid JSON: {e}")

    errors: list = []
    title = deck.get("title", "")
    if not isinstance(title, str) or not (1 <= len(title) <= 200):
        errors.append("title is required (1-200 chars)")
    description = deck.get("description", "")
    if len(description) > 1000:
        errors.append("description exceeds 1000 chars")
    tags = deck.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 10:
        errors.append("tags must be a list of at most 10 strings")
    cards_in = deck.get("cards")
    if not isinstance(cards_in, list) or not cards_in:
        fail("deck.json must contain a non-empty 'cards' array")

    media: dict = {}
    cards_out = []
    for i, card in enumerate(cards_in):
        where = f"cards[{i}]"
        front = card.get("frontContent", "")
        back = card.get("backContent", "")
        if not isinstance(front, str) or not isinstance(back, str):
            errors.append(f"{where}: frontContent/backContent must be strings")
            continue
        front_secs, back_secs = [], []
        for side, key, out in (("front", "frontSections", front_secs), ("back", "backSections", back_secs)):
            for j, sec in enumerate(card.get(key) or []):
                norm = normalize_section(sec, deck_dir, f"{where}.{key}[{j}]", errors, media)
                if norm:
                    norm["position"] = len(out)
                    out.append(norm)
        if not front and not front_secs:
            errors.append(f"{where}: card front is empty (no text, no sections)")
        if not back and not back_secs:
            errors.append(f"{where}: card back is empty (no text, no sections)")
        cards_out.append({
            "id": str(uuid.uuid4()),
            "position": i,
            "frontContent": front,
            "backContent": back,
            "frontSections": front_secs,
            "backSections": back_secs,
        })

    if errors:
        fail("deck.json validation failed:\n  - " + "\n  - ".join(errors))

    manifest = {
        "schemaVersion": 1,
        "deckTitle": title,
        "description": description,
        "language": deck.get("language", "zh-TW"),
        "tags": tags,
        "cardCount": len(cards_out),
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "builtBy": "memory-toast-make-card",
    }

    build_dir = deck_dir / "build"
    build_dir.mkdir(exist_ok=True)
    zip_path = build_dir / "pack.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("cards.json", json.dumps({"schemaVersion": 1, "cards": cards_out}, ensure_ascii=False, indent=2))
        for zip_name, src in media.items():
            zf.write(src, zip_name)

    data = zip_path.read_bytes()
    if len(data) > MAX_ZIP_BYTES:
        fail(f"ZIP is {len(data)} bytes — exceeds the 100MB server limit")
    return {
        "zip_path": zip_path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "card_count": len(cards_out),
        "media_count": len(media),
        "title": title,
        "description": description,
        "tags": tags,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("deck_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--deck-id", help="upload to an existing deck instead of creating one")
    parser.add_argument("--local-version", type=int, default=0,
                        help="current server pack version of the deck (for --deck-id updates)")
    parser.add_argument("--force", action="store_true", help="overwrite server pack on version conflict")
    parser.add_argument("--api", help="override API base URL")
    args = parser.parse_args()

    deck_dir = args.deck_dir.resolve()
    if not deck_dir.is_dir():
        fail(f"deck dir not found: {deck_dir}")

    pack = build_pack(deck_dir)
    print(f"Built {pack['zip_path']}")
    print(f"  cards: {pack['card_count']}  media: {pack['media_count']}  "
          f"size: {pack['size']:,} bytes  sha256: {pack['sha256']}")
    if args.dry_run:
        print("Dry run — not uploading.")
        return

    api = resolve_api_url(args.api)
    token = get_access_token(api)
    print(f"Authenticated to {api}")

    if args.deck_id:
        deck_id = args.deck_id
    else:
        body = {"title": pack["title"]}
        if pack["description"]:
            body["description"] = pack["description"]
        if pack["tags"]:
            body["tags"] = pack["tags"]
        status, res = api_call("POST", f"{api}/api/v1/decks", body, token)
        if status != 201:
            fail(f"create deck failed ({status}): {res}")
        deck_id = res["deck"]["id"]
        print(f"Created deck {deck_id} ({pack['title']})")

    sync_url = f"{api}/api/v1/decks/{deck_id}/sync"
    if args.force:
        sync_url += "?force=true"
    status, res = api_call("POST", sync_url, {
        "localVersion": args.local_version,
        "size": pack["size"],
        "sha256": pack["sha256"],
        "cardCount": pack["card_count"],
    }, token)
    if status == 409:
        fail(
            f"version conflict: server is at version {res.get('serverVersion')}, "
            f"you sent {res.get('localVersion')}.\n"
            f"Re-run with --local-version {res.get('serverVersion')} (or --force to overwrite)."
        )
    if status != 200:
        fail(f"sync start failed ({status}): {res}")
    upload_url, pack_id, r2_key = res["uploadUrl"], res["packId"], res["r2Key"]

    print(f"Uploading {pack['size']:,} bytes to R2…")
    status, res = api_call("PUT", upload_url, raw=pack["zip_path"].read_bytes())
    if status not in (200, 201):
        fail(f"R2 upload failed ({status}): {res}")

    status, res = api_call("POST", f"{api}/api/v1/packs/{pack_id}/commit", {
        "expectedSize": pack["size"],
        "sha256": pack["sha256"],
        "cardCount": pack["card_count"],
        "r2Key": r2_key,
    }, token)
    if status != 200:
        fail(f"commit failed ({status}): {res}")

    version = res.get("pack", {}).get("version")
    print(f"Done. deck={deck_id} pack={pack_id} version={version}")
    print("Open the app deck list and pull the deck to see it on device.")


if __name__ == "__main__":
    main()
