# Memory Toast pack format & upload protocol

Authoritative spec for the `memory-toast-make-card` skill: the ZIP pack structure, the
`deck.json` input format, and the full API upload flow. Read this before writing
`deck.json` or debugging an upload.

## Contents

1. [ZIP pack structure](#1-zip-pack-structure)
2. [cards.json schema](#2-cardsjson-schema)
3. [deck.json — the skill's input format](#3-deckjson--the-skills-input-format)
4. [Upload protocol (API)](#4-upload-protocol-api)
5. [Versioning & conflicts](#5-versioning--conflicts)
6. [Limits & validation](#6-limits--validation)
7. [Preview packs (試讀本)](#7-preview-packs-試讀本)

## 1. ZIP pack structure

Each deck's full content is packaged into a single ZIP. The server stores deck metadata
and pack-version records only — it never parses the ZIP body.

```
pack.zip
├── manifest.json        # deck-level metadata
├── cards.json           # all cards + sections
└── media/               # local media files, named {sectionId}.{ext}
    ├── 3f2a…c1.jpg
    └── 9b07…e4.mp3
```

`manifest.json` (provenance only — the app does not validate `builtBy`):

```json
{
  "schemaVersion": 1,
  "deckTitle": "Japanese N3 Verbs",
  "description": "…",
  "language": "zh-TW",
  "tags": ["japanese", "N3"],
  "cardCount": 50,
  "createdAt": "2026-06-14T08:00:00Z",
  "builtBy": "memory-toast-make-card"
}
```

## 2. cards.json schema

```json
{
  "schemaVersion": 1,
  "cards": [
    {
      "id": "<uuid>",
      "position": 0,
      "frontContent": "front text (may be empty, but not empty at the same time as sections)",
      "backContent": "back text",
      "frontSections": [
        {
          "id": "<uuid>",
          "position": 0,
          "kind": "image",            // image | audio | video
          "storageKind": "local",     // local (file in ZIP) | external (URL)
          "storageRef": "media/<sectionId>.jpg",  // local: path in ZIP; external: full URL
          "caption": "caption text or null",
          "mimeType": "image/jpeg",   // required for local; may be null for external
          "durationMs": null          // audio/video length, may be null
        }
      ],
      "backSections": []
    }
  ]
}
```

Rules:

- `id` is a UUID v4. **On re-upload of the same deck, `upload_pack.py` matches each card against the previous build (`build/pack.zip`) by content and reuses the existing id for unchanged cards** (including back-only edits, e.g. adding an example) — only new cards or cards whose front text changed get a fresh UUID. This keeps the user's study progress across deck updates (the app keys study progress by card id and prunes orphaned progress on pull). `position` starts at 0 and increments by array order.
- For `storageKind: local`, `storageRef` must be `media/{sectionId}.{ext}` and the ZIP must
  contain the matching file.
- `storageKind: external` is for YouTube/Vimeo and similar links; `storageRef` is the full URL.
- Each section needs at least one of `storageRef` / `caption` non-empty (the app's model
  throws otherwise).
- For each card side: the text (`frontContent`/`backContent`) and that side's sections cannot
  both be empty.

## 3. deck.json — the skill's input format

`scripts/upload_pack.py` consumes a simplified format (it generates every UUID, position,
`storageRef`, and `mimeType`). Put it in a "deck directory" with media referenced by
relative path:

```
my-deck/
├── deck.json
└── assets/
    ├── taberu.jpg          # e.g. an image you generated with gen_image.py
    └── pronounce.mp3
```

```json
{
  "title": "Japanese N3 Verbs",
  "description": "50 common verbs",
  "language": "zh-TW",
  "tags": ["japanese", "N3"],
  "cards": [
    {
      "frontContent": "食べる",
      "backContent": "to eat (taberu)",
      "frontSections": [
        { "kind": "image", "file": "assets/taberu.jpg", "caption": "optional" }
      ],
      "backSections": [
        { "kind": "audio", "file": "assets/pronounce.mp3" },
        { "kind": "video", "url": "https://www.youtube.com/watch?v=…" }
      ]
    }
  ]
}
```

- `language` — the **audience / explanation language** the card backs are written in
  (who the deck is _for_, **not** what it teaches). One of `en` `zh-TW` `zh-Hans` `ja` `es` `vi`
  (the app's UI locales); anything off-list or omitted becomes `en`. Set it on publish with
  `library_pack.py publish --language`, or it carries over from this `deck.json`.
- Each section must have exactly one of `file` (local → `storageKind: local`) or `url`
  (→ `external`).
- Extension whitelist — image: jpg/jpeg/png/gif/webp; audio: mp3/m4a/wav/aac/ogg;
  video: mp4/mov/webm.
- Text-only cards need just `frontContent` + `backContent`; sections may be omitted.
- Generated images (from `gen_image.py`) are just local image sections — save them under the
  deck directory and reference them via `file`.

### 3.1 Rich text — the HTML subset

`frontContent` / `backContent` and a section's `caption` may be **HTML-subset** strings that
carry **font size, bold / italic / underline, color, and paragraph alignment**. The mobile app
parses them with the same whitelist; anything outside it is dropped (tag removed, inner text
kept), so as long as you stay inside the whitelist, what you emit is what the app renders.

Whitelist (identical to `apps/mobile/.../rich_text/rich_html_parser.dart`):

| Class | Allowed | Meaning |
|-------|---------|---------|
| inline | `<b>` `<strong>` | bold |
| inline | `<i>` `<em>` | italic |
| inline | `<u>` | underline |
| inline | `<br>` | line break |
| inline | `<span style="…">` | **only** `font-size:sm\|base\|lg\|xl` and/or `color:<token>` |
| block  | `<p style="text-align:left\|center\|right">…</p>` | paragraph alignment |

- `color` token must be one of: `primary` `red` `orange` `green` `blue` `purple` `gray`.
- Any other tag (`<div>`, `<script>`, …), other attribute (`class`, `onclick`, …), or other
  style prop/value (`font-weight`, `99px`, `hotpink`, …) is dropped while its text is kept.

How the emitter derives `*Html` + plain text:

- The rich source for a field is the explicit `frontContentHtml` / `backContentHtml` /
  `captionHtml` key on the card/section if present; otherwise the `frontContent` /
  `backContent` / `caption` value itself if it contains whitelist tags.
- With a rich source: the output `*Html` = sanitized whitelist HTML, and the plain field =
  the tag-stripped text (used for search + as a render fallback).
- With no rich source: only the plain field is emitted; the `*Html` key is **omitted** so
  old-style plain packs stay byte-clean.
- The empty-card check (front/back + sections not all empty) runs against the **plain** text.

Rich card in deck.json (the two forms are equivalent — use either):

```json
{
  "frontContent": "<b>食べる</b><br><span style=\"font-size:lg;color:red\">godan verb</span>",
  "backContent": "to eat (taberu)"
}
```

```json
{
  "frontContent": "食べる",
  "frontContentHtml": "<b>食べる</b><br><span style=\"font-size:lg;color:red\">godan verb</span>",
  "backContent": "to eat (taberu)"
}
```

Both produce, in `cards.json`:
`frontContentHtml = "<b>食べる</b><br><span style=\"font-size:lg;color:red\">godan verb</span>"`
and `frontContent = "食べる\ngodan verb"` (the plain projection).

### 3.2 Ordered blocks — advanced layout

Most cards only need `frontContent` + `frontSections[]`. To control the **exact vertical
order** (e.g. "word → audio right under it → KK/POS → a tappable example → image"), a card
side may instead use `frontBlocks` / `backBlocks`: an **ordered** array of blocks. The app
renders blocks in order when present; cards without blocks render via `*Content` +
`*Sections` exactly as before.

Block types:

| type | fields | notes |
|------|--------|-------|
| `text` | `content` / `contentHtml` (rich, §3.1 whitelist), `audios[]` (optional), `audioAlign` / `audioSize` (optional) | a text run; may carry a row of tap-to-play audio buttons below it |
| `image` | `file` (local) or `url` (external), `caption` / `captionHtml` (optional) | image, same rules as a §3 image section |

Each `audios[]` button: `{ "label": "US", "file": "assets/x.us.mp3" }` (or `url`); `label`
may be rich (`labelHtml`). Each button is independently tap-to-play — no autoplay, no toggle;
add more buttons for more accents.

The audio row's layout is tunable per text block: `audioAlign` = `start` / `center` (default) /
`end` (horizontal alignment of the button row), and `audioSize` = `sm` / `md` (default) / `lg`
(button padding, icon and label size). Older app versions ignore these and use the defaults.

deck.json example (back uses blocks; front stays a plain word):

```json
{
  "frontContent": "<p style=\"text-align:center\"><b><span style=\"font-size:xl\">apple</span></b></p>",
  "backBlocks": [
    { "type": "text", "content": "<b><span style=\"font-size:xl\">apple</span></b>",
      "audios": [ {"label":"US","file":"assets/apple.us.mp3"}, {"label":"UK","file":"assets/apple.uk.mp3"} ],
      "audioAlign": "center", "audioSize": "md" },
    { "type": "text", "content": "<span style=\"color:gray\">[ˈæpl̩]</span><br><b><span style=\"color:primary\">n.</span></b> apple" },
    { "type": "text", "content": "<b>e.g.</b> I eat an <b>apple</b>.",
      "audios": [ {"label":"US","file":"assets/apple.ex1.us.mp3"}, {"label":"UK","file":"assets/apple.ex1.uk.mp3"} ] },
    { "type": "image", "file": "assets/apple.webp" }
  ]
}
```

**Backward compatibility (important):** when emitting blocks, `upload_pack.py` ALSO writes an
equivalent legacy projection — `backContent` (text blocks joined) + `backSections` (image
blocks and every audio turned into a section, caption = the button's label). So new app
versions render the new layout from `*Blocks`, while old app versions ignore `*Blocks` and
render the previous layout from `*Content` + `*Sections`. `schemaVersion` stays `1` (purely
additive optional keys). Each block's media follows the same extension whitelist and gets
generated UUID/position/storageRef/mimeType, just like §3 sections.

## 4. Upload protocol (API)

`scripts/upload_pack.py` runs these steps (the same endpoints the mobile app's sync uses):

| Step | Endpoint | Notes |
|------|----------|-------|
| 1. auth | `POST /api/v1/auth/refresh` `{refreshToken}` | mints a 15-min `accessToken`; run `mt_login.py` once to obtain the stored refresh token |
| 2. create deck | `POST /api/v1/decks` `{title, description?, tags?}` | returns `deck.id`; skipped when updating an existing deck |
| 3. start sync | `POST /api/v1/decks/{deckId}/sync` `{localVersion, size, sha256, cardCount}` | returns an R2 signed PUT URL (10-min) + `packId` + `r2Key` |
| 4. upload ZIP | `PUT {uploadUrl}` (body = ZIP bytes) | query-signed URL, no extra header |
| 5. commit | `POST /api/v1/packs/{packId}/commit` `{expectedSize, sha256, cardCount, r2Key}` | server verifies the R2 object exists and matches → writes the pack row, bumps `decks.current_pack_id` |

Every request except step 4 carries `Authorization: Bearer {accessToken}`.

After commit the deck appears in the app's deck list; entering it shows a "new version
available v.N" banner with a **Download** button (the app compares the server pack version
with the local one and offers the pull when the server is newer and no local edits are pending).

## 5. Versioning & conflicts

- New deck, first pack: `localVersion: 0`, which becomes version 1 after commit.
- Updating an existing deck: `localVersion` must equal the server's current version, or sync
  returns **409** `{error: "version_mismatch", serverVersion, localVersion}`.
- Resolve a 409: re-run with `--local-version {serverVersion}` (confirming you want to
  overwrite server content), or `--force` (skips the version check).
- **Important:** an upload replaces the server pack wholesale. If the phone has un-synced local
  edits, push them from the app first ("同步"), then update with the new version number —
  otherwise those edits are overwritten on the next pull.

## 6. Limits & validation

| Item | Limit |
|------|-------|
| ZIP size | ≤ 300 MB |
| title | 1–200 characters |
| description | ≤ 1000 characters |
| tags | ≤ 10 |
| sha256 | 64 lowercase hex; must match the ZIP at commit |
| expectedSize | must equal the actual R2 object size, or commit returns `size_mismatch` and the object is deleted |
| signed PUT URL | expires in 10 minutes — upload immediately after sync |

## 7. Preview packs (試讀本)

A **preview pack (試讀本)** is a free sample of a paid pack: it is **its own deck + its own
`library_packs` row**, linked back to the full pack by a single field. Because it is a separate
deck, study/training is automatically separate — no extra isolation logic is needed. Previews are
produced by the `library_pack.py preview` command (see SKILL.md). Rules:

- Default = the full deck's **first 10 cards**; `--cards` (1-based indices and/or card ids,
  comma-separated) overrides, **hard-capped at 10**.
- The **full deck must have MORE than 50 cards** to make a preview (skill-side gate; error message
  `卡包需超過 50 張才能製作試讀本`).
- A preview is **always free** (`priceTokens = 0`).
- A full pack may have **one** published preview; a preview **cannot itself have a preview**.

`preview` reuses the existing `upload_pack.py` (to build the preview's own deck) and the
`library/publish` endpoint, with one extra field on publish:

| Field | Where | Meaning |
|-------|-------|---------|
| `previewOfLibraryPackId` | body of `POST /api/v1/library/publish` | When set, this new library pack is the preview of the full pack identified by `previewOfLibraryPackId`. The server verifies: the caller owns the target full pack, the target is not itself a preview (no nesting), the full pack has no published preview yet, the full pack's `cardCount > 50`, this pack's `cardCount <= 10`, and forces `priceTokens = 0`. |

Links written into `.memory-toast.json` (auto-written by `preview`, reused on the next update —
do not hand-edit the ids):

- The **full deck's record** gains `previewDeckId` and `previewLibraryPackId` (reverse lookup to
  its preview).
- The **preview's record** gains `previewOfLibraryPackId` (back-pointer to the full pack), plus its
  own `deckId` / `libraryPackId` / `version` as usual.

Re-running `preview`: if the preview's record already has a `libraryPackId`, it `release`s a new
version instead of publishing a duplicate. A new version of the full pack does **not** auto-update
the preview — re-run `preview` to refresh it.
