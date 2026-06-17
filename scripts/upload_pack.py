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
  (default)          If DECK_DIR has a .memory-toast.json (written by a previous upload),
                     UPDATE that deck automatically (deckId + version from the record).
                     Otherwise create a NEW deck and upload pack v1.
  --deck-id ID       Upload to a specific existing deck (overrides the record).
  --local-version N  Override the server pack version (else taken from the record).
  --new              Force-create a new deck even if a record exists in DECK_DIR.
  --force            Overwrite the server pack on a version conflict.

On every successful upload, DECK_DIR/.memory-toast.json is (re)written with deckId,
version, packId, title, card count and a structure summary — the "AI record" a future
session reads to update or publish (library_pack.py) the deck.
"""

import argparse
import hashlib
import html
import json
import mimetypes
import uuid
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from _mt_auth import api_call, fail, get_access_token, resolve_api_url

MAX_ZIP_BYTES = 200 * 1024 * 1024  # server-side limit in validators/sync.ts
DECK_RECORD = ".memory-toast.json"  # per-deck AI state file (deckId, version, libraryPackId, …)

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

# ---------------------------------------------------------------------------
# Rich-text HTML subset (mirrors the Dart whitelist in
# apps/mobile/lib/features/cards/data/rich_text/rich_html_{parser,serializer}.dart).
#
# Card text fields and section captions may carry an OPTIONAL `*Html` value using
# this subset. Anything outside the whitelist is dropped (tag removed, inner text
# kept) exactly as the mobile parser does, so what we emit is what the app renders.
#
# Whitelist:
#   inline: <b>/<strong>=bold, <i>/<em>=italic, <u>=underline, <br>=line break
#   <span style="..."> with ONLY font-size:sm|base|lg|xl and/or color:<token>
#       where token in {primary,red,orange,green,blue,purple,gray}
#   block:  <p style="text-align:left|center|right">…</p>
# ---------------------------------------------------------------------------
_INLINE_TAGS = {"b", "strong", "i", "em", "u"}  # plain pass-through inline tags
_SIZE_TOKENS = {"sm", "base", "lg", "xl"}
_COLOR_TOKENS = {"primary", "red", "orange", "green", "blue", "purple", "gray"}
_ALIGN_TOKENS = {"left", "center", "right"}
# Any whitelist tag, used by _has_rich_tags.
_ALL_RICH_TAGS = _INLINE_TAGS | {"br", "span", "p"}


def _parse_style_attr(style):
    """Parse an inline style attribute into a lowercased prop -> value dict.

    Mirrors _parseStyleAttr in rich_html_parser.dart.
    """
    out = {}
    if not style:
        return out
    for decl in style.split(";"):
        idx = decl.find(":")
        if idx <= 0:
            continue
        key = decl[:idx].strip().lower()
        value = decl[idx + 1:].strip().lower()
        if key and value:
            out[key] = value
    return out


def _has_rich_tags(s: str) -> bool:
    """True if the string contains any whitelisted rich-text tag."""
    if not s or "<" not in s:
        return False

    found = {"hit": False}

    class _Detect(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag.lower() in _ALL_RICH_TAGS:
                found["hit"] = True

        def handle_startendtag(self, tag, attrs):
            if tag.lower() in _ALL_RICH_TAGS:
                found["hit"] = True

    p = _Detect(convert_charrefs=True)
    try:
        p.feed(s)
        p.close()
    except Exception:
        return False
    return found["hit"]


def _span_style_for(attrs) -> str:
    """Return the canonical `font-size...;color...` style for a <span>, or ''.

    Keeps only whitelisted font-size + color; '' means the span carries no
    allowed styling and should be unwrapped (text kept).
    """
    style_val = None
    for k, v in attrs:
        if k.lower() == "style":
            style_val = v
    styles = _parse_style_attr(style_val)
    parts = []
    size = styles.get("font-size")
    if size in _SIZE_TOKENS:
        parts.append(f"font-size:{size}")
    color = styles.get("color")
    if color in _COLOR_TOKENS:
        parts.append(f"color:{color}")
    return ";".join(parts)


def _p_align_for(attrs) -> str:
    """Return the whitelisted text-align value for a <p>, or '' for default/left."""
    style_val = None
    for k, v in attrs:
        if k.lower() == "style":
            style_val = v
    align = _parse_style_attr(style_val).get("text-align")
    return align if align in _ALIGN_TOKENS else ""


def _esc(text: str) -> str:
    """Canonical text escaping (matches _escape in rich_html_serializer.dart)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _sanitize_rich_html(s: str) -> str:
    """Drop everything outside the whitelist (keeping inner text) and emit
    canonical whitelist HTML. Mirrors the mobile parser's tolerance: unknown
    tags/attrs/style-props/values are removed but their text is preserved.
    """

    class _Sanitizer(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out = []
            # Stack of what we actually emitted for each open tag so end tags can
            # close the right thing (or nothing, when a tag was unwrapped).
            self.stack = []

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag == "br":
                # Void element: HTMLParser reports it via starttag, not startendtag.
                self.out.append("<br>")
            elif tag in _INLINE_TAGS:
                self.out.append(f"<{tag}>")
                self.stack.append(tag)
            elif tag == "span":
                style = _span_style_for(attrs)
                if style:
                    self.out.append(f'<span style="{style}">')
                    self.stack.append("span")
                else:
                    self.stack.append("")  # unwrap: keep text, no tag
            elif tag == "p":
                align = _p_align_for(attrs)
                if align:
                    self.out.append(f'<p style="text-align:{align}">')
                else:
                    self.out.append("<p>")
                self.stack.append("p")
            else:
                # Unknown/disallowed tag: drop the tag, keep its text content.
                self.stack.append("")

        def handle_startendtag(self, tag, attrs):
            if tag.lower() == "br":
                self.out.append("<br>")
            # Any other self-closing tag is dropped entirely (no text).

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag == "br":
                return
            if not self.stack:
                return
            emitted = self.stack.pop()
            if emitted:
                self.out.append(f"</{emitted}>")

        def handle_data(self, data):
            self.out.append(_esc(data))

    p = _Sanitizer()
    p.feed(s)
    p.close()
    # Close any tags left open by malformed input.
    while p.stack:
        emitted = p.stack.pop()
        if emitted:
            p.out.append(f"</{emitted}>")
    return "".join(p.out)


def _strip_tags(s: str) -> str:
    """Plain-text projection of rich HTML.

    Mirrors RichDoc.toPlainText(): blocks joined by '\\n'. <br> and </p> become
    newlines, all other tags are removed, entities are unescaped. A leading <p>
    does not add a newline (the first block has no preceding separator).
    """

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self._block_open = False

        def _newline(self):
            self.parts.append("\n")

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag == "br":
                self._newline()
            elif tag == "p":
                # A new block starts; separate from the previous block's text.
                if self._block_open or "".join(self.parts).strip("\n"):
                    self._newline()
                self._block_open = True

        def handle_startendtag(self, tag, attrs):
            if tag.lower() == "br":
                self._newline()

        def handle_endtag(self, tag):
            # </p> is not needed as a separator — the next <p> inserts one.
            pass

        def handle_data(self, data):
            self.parts.append(data)

    p = _Stripper()
    p.feed(s)
    p.close()
    return "".join(p.parts)


def _rich_source(obj: dict, plain_key: str, html_key: str):
    """Return the rich HTML source for a field, or None if the field is plain.

    Rich source is: an explicit `*Html` key if present; else the plain value if
    it itself contains whitelist tags.
    """
    explicit = obj.get(html_key)
    if isinstance(explicit, str) and explicit:
        return explicit
    plain = obj.get(plain_key)
    if isinstance(plain, str) and _has_rich_tags(plain):
        return plain
    return None


def summarize_structure(cards_out: list) -> dict:
    """Auto-derived description of the deck's card shape, for the AI record."""
    front_kinds = sorted({s["kind"] for c in cards_out for s in c["frontSections"]})
    back_kinds = sorted({s["kind"] for c in cards_out for s in c["backSections"]})
    return {
        "front": {"text": any(c["frontContent"] for c in cards_out), "sections": front_kinds},
        "back": {"text": any(c["backContent"] for c in cards_out), "sections": back_kinds},
    }


def read_record(deck_dir: Path) -> dict:
    """Read the per-deck .memory-toast.json state file (empty dict if absent/bad)."""
    path = deck_dir / DECK_RECORD
    if path.is_file():
        try:
            return json.loads(path.read_text() or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def write_record(deck_dir: Path, fields: dict) -> Path:
    """Merge fields into .memory-toast.json, preserving existing keys (e.g. libraryPackId)."""
    rec = read_record(deck_dir)
    rec.update({k: v for k, v in fields.items() if v is not None})
    rec["_note"] = (
        "make-card state for this deck. A future AI session reads this to UPDATE the deck "
        "(deckId + version) or release it to the Library (libraryPackId). Auto-written by "
        "upload_pack.py / library_pack.py — safe to read; do not hand-edit the ids."
    )
    path = deck_dir / DECK_RECORD
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    return path


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

    out = {
        "id": sec_id,
        "kind": kind,
        "storageKind": storage_kind,
        "storageRef": storage_ref,
        "caption": caption if caption else None,
        "mimeType": mime,
        "durationMs": sec.get("durationMs"),
    }
    # Optional rich caption: emit captionHtml only when there is rich content;
    # caption always holds the tag-stripped plain text.
    cap_src = _rich_source(sec, "caption", "captionHtml")
    if cap_src is not None:
        out["captionHtml"] = _sanitize_rich_html(cap_src)
        plain = _strip_tags(cap_src)
        out["caption"] = plain if plain else None
    return out


def normalize_audio_ref(a: dict, deck_dir: Path, where: str, errors: list, media: dict) -> dict:
    """One tap-to-play audio button inside a text block -> cards.json audio ref.

    Mirrors an audio section (storageKind/storageRef/mimeType/durationMs) plus a short
    `label` (optionally rich via labelHtml). Local files get a generated media/{id}{ext}
    storageRef registered in `media`.
    """
    aid = str(uuid.uuid4())
    label = a.get("label")
    label_html = None
    lab_src = _rich_source(a, "label", "labelHtml")
    if lab_src is not None:
        label_html = _sanitize_rich_html(lab_src)
        label = _strip_tags(lab_src) or None
    has_file = bool(a.get("file"))
    has_url = bool(a.get("url"))
    if has_file == has_url:
        errors.append(f"{where}: exactly one of 'file' or 'url' is required")
        return {}
    if has_url:
        url = a["url"]
        if not url.startswith(("http://", "https://")):
            errors.append(f"{where}: url must be http(s), got {url!r}")
            return {}
        storage_kind, storage_ref, mime = "external", url, None
    else:
        rel = a["file"]
        src = (deck_dir / rel).resolve()
        if not src.is_file():
            errors.append(f"{where}: file not found: {rel}")
            return {}
        ext = src.suffix.lower()
        if ext not in ALLOWED_EXTS["audio"]:
            errors.append(f"{where}: extension {ext!r} not allowed for audio")
            return {}
        storage_kind = "local"
        storage_ref = f"media/{aid}{ext}"
        mime = guess_mime(ext)
        media[storage_ref] = src
    out = {
        "id": aid,
        "label": label if label else None,
        "storageKind": storage_kind,
        "storageRef": storage_ref,
        "mimeType": mime,
        "durationMs": a.get("durationMs"),
    }
    if label_html is not None:
        out["labelHtml"] = label_html
    return out


def normalize_blocks(blocks: list, deck_dir: Path, where: str, errors: list, media: dict):
    """Process one side's ordered blocks (deck.json input) into cards.json blocks plus a
    legacy projection (plain text, rich html, sections) so OLD app versions still render.

    Returns (blocks_out, plain, html, sections).
      - Block types: 'text' (content/contentHtml + optional audios[]) and 'image'.
      - Legacy projection: text blocks -> joined content/html; image blocks + every audio
        -> sections (audio caption = its label), in block order.
    """
    blocks_out, plains, htmls, sections = [], [], [], []
    for j, blk in enumerate(blocks):
        w = f"{where}[{j}]"
        if not isinstance(blk, dict):
            errors.append(f"{w}: block must be an object")
            continue
        btype = blk.get("type")
        if btype == "text":
            content = blk.get("content", "")
            if not isinstance(content, str):
                errors.append(f"{w}: text block 'content' must be a string")
                content = ""
            src = _rich_source(blk, "content", "contentHtml")
            if src is not None:
                bhtml = _sanitize_rich_html(src)
                bplain = _strip_tags(src)
            else:
                bhtml = None
                bplain = content
            audios_out = []
            for k, a in enumerate(blk.get("audios") or []):
                na = normalize_audio_ref(a, deck_dir, f"{w}.audios[{k}]", errors, media)
                if na:
                    audios_out.append(na)
            if not bplain and not audios_out:
                errors.append(f"{w}: text block is empty (no content, no audios)")
            bo = {"type": "text", "position": len(blocks_out), "content": bplain}
            if bhtml is not None:
                bo["contentHtml"] = bhtml
            if audios_out:
                bo["audios"] = audios_out
            # Optional audio-row layout hints (blocks model only; the legacy
            # projection has no equivalent). audioAlign: start|center|end,
            # audioSize: sm|md|lg.
            for hint in ("audioAlign", "audioSize"):
                if blk.get(hint):
                    bo[hint] = blk[hint]
            blocks_out.append(bo)
            if bplain:
                plains.append(bplain)
            htmls.append(bhtml if bhtml is not None else _esc(bplain))
            for na in audios_out:
                sec = {
                    "id": na["id"], "kind": "audio", "position": len(sections),
                    "storageKind": na["storageKind"], "storageRef": na["storageRef"],
                    "caption": na.get("label"), "mimeType": na["mimeType"],
                    "durationMs": na.get("durationMs"),
                }
                if na.get("labelHtml"):
                    sec["captionHtml"] = na["labelHtml"]
                sections.append(sec)
        elif btype == "image":
            sec = normalize_section(
                {"kind": "image", "file": blk.get("file"), "url": blk.get("url"),
                 "caption": blk.get("caption"), "captionHtml": blk.get("captionHtml")},
                deck_dir, w, errors, media)
            if sec:
                bo = {"type": "image", "position": len(blocks_out),
                      "storageKind": sec["storageKind"], "storageRef": sec["storageRef"],
                      "mimeType": sec["mimeType"], "caption": sec.get("caption")}
                if sec.get("captionHtml"):
                    bo["captionHtml"] = sec["captionHtml"]
                blocks_out.append(bo)
                sec["position"] = len(sections)
                sections.append(sec)
        else:
            errors.append(f"{w}: block 'type' must be 'text' or 'image', got {btype!r}")
    plain = "\n".join(plains)
    html = "<br><br>".join(h for h in htmls if h)
    return blocks_out, plain, html, sections


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
        # Each side independently: use ordered `*Blocks` when present (new model),
        # else the legacy `*Content` + `*Sections` path. Block sides also emit a
        # legacy projection (plain/html/sections) so OLD app versions still render.
        side_out = {}  # side -> (plain, html, sections, blocks_out_or_None)
        for side, ckey, hkey, skey, bkey in (
            ("front", "frontContent", "frontContentHtml", "frontSections", "frontBlocks"),
            ("back", "backContent", "backContentHtml", "backSections", "backBlocks"),
        ):
            blocks_in = card.get(bkey)
            if isinstance(blocks_in, list) and blocks_in:
                b_out, plain, joined_html, secs = normalize_blocks(
                    blocks_in, deck_dir, f"{where}.{bkey}", errors, media)
                side_out[side] = (plain, joined_html or None, secs, b_out)
                continue
            content = card.get(ckey, "")
            if not isinstance(content, str):
                errors.append(f"{where}: {ckey} must be a string")
                content = ""
            # Rich source = explicit *Html key, else the plain value if it has
            # whitelist tags. When rich, *Html holds sanitized HTML and the plain
            # field holds the tag-stripped projection (search + render fallback).
            chtml = None
            src = _rich_source(card, ckey, hkey)
            if src is not None:
                chtml = _sanitize_rich_html(src)
                content = _strip_tags(src)
            secs = []
            for j, sec in enumerate(card.get(skey) or []):
                norm = normalize_section(sec, deck_dir, f"{where}.{skey}[{j}]", errors, media)
                if norm:
                    norm["position"] = len(secs)
                    secs.append(norm)
            side_out[side] = (content, chtml, secs, None)

        front, front_html, front_secs, front_blocks = side_out["front"]
        back, back_html, back_secs, back_blocks = side_out["back"]
        # Empty check runs against the PLAIN text (rich markup that strips to
        # nothing counts as empty).
        if not front and not front_secs:
            errors.append(f"{where}: card front is empty (no text, no sections)")
        if not back and not back_secs:
            errors.append(f"{where}: card back is empty (no text, no sections)")
        card_out = {
            "id": str(uuid.uuid4()),
            "position": i,
            "frontContent": front,
            "backContent": back,
            "frontSections": front_secs,
            "backSections": back_secs,
        }
        if front_html is not None:
            card_out["frontContentHtml"] = front_html
        if back_html is not None:
            card_out["backContentHtml"] = back_html
        if front_blocks is not None:
            card_out["frontBlocks"] = front_blocks
        if back_blocks is not None:
            card_out["backBlocks"] = back_blocks
        cards_out.append(card_out)

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
        fail(f"ZIP is {len(data)} bytes — exceeds the {MAX_ZIP_BYTES // (1024*1024)}MB server limit")
    return {
        "zip_path": zip_path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "card_count": len(cards_out),
        "media_count": len(media),
        "title": title,
        "description": description,
        "tags": tags,
        "language": deck.get("language", "zh-TW"),
        "structure": summarize_structure(cards_out),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("deck_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--deck-id", help="upload to an existing deck instead of creating one")
    parser.add_argument("--local-version", type=int, default=None,
                        help="current server pack version (auto-read from .memory-toast.json if present)")
    parser.add_argument("--force", action="store_true", help="overwrite server pack on version conflict")
    parser.add_argument("--new", action="store_true",
                        help="force-create a NEW deck even if .memory-toast.json exists in DECK_DIR")
    parser.add_argument("--api", help="override API base URL")
    args = parser.parse_args()

    deck_dir = args.deck_dir.resolve()
    if not deck_dir.is_dir():
        fail(f"deck dir not found: {deck_dir}")

    pack = build_pack(deck_dir)
    print(f"Built {pack['zip_path']}")
    print(f"  cards: {pack['card_count']}  media: {pack['media_count']}  "
          f"size: {pack['size']:,} bytes  sha256: {pack['sha256']}")

    # Resolve update-vs-create from the per-deck AI record (.memory-toast.json).
    record = read_record(deck_dir)
    deck_id = args.deck_id or (None if args.new else record.get("deckId"))
    if args.local_version is not None:
        local_version = args.local_version
    elif deck_id and deck_id == record.get("deckId"):
        local_version = record.get("version", 0)  # auto-update from the record
    else:
        local_version = 0

    if args.dry_run:
        if deck_id:
            print(f"Dry run — real run would UPDATE deck {deck_id} (localVersion {local_version}).")
        else:
            print("Dry run — real run would CREATE a new deck.")
        return

    api = resolve_api_url(args.api)
    token = get_access_token(api)
    print(f"Authenticated to {api}")

    if deck_id:
        if not args.deck_id:
            print(f"Found {DECK_RECORD} → updating deck {deck_id} (server v{local_version})")
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
        "localVersion": local_version,
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

    write_record(deck_dir, {
        "deckId": deck_id,
        "packId": pack_id,
        "version": version,
        "title": pack["title"],
        "language": pack["language"],
        "cardCount": pack["card_count"],
        "structure": pack["structure"],
        "apiUrl": api,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "builtBy": "memory-toast-make-card",
    })
    print(f"Wrote {DECK_RECORD} — next `upload_pack.py {deck_dir.name}` will auto-update this deck.")
    print("Open the app deck list and pull the deck to see it on device.")


if __name__ == "__main__":
    main()
