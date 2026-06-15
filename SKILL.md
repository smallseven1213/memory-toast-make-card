---
name: memory-toast-make-card
description: Create Memory Toast flashcard decks (卡包) from the user's requirements, web research, or their own data (PDFs, images, notes) — optionally generating card images with the user's own OpenAI/Gemini key — then upload them to the user's Memory Toast account as ZIP packs via the API. Use when the user asks to make cards, build a deck/卡包, turn study material into flashcards, generate card images, or upload a deck to Memory Toast.
---

# Make Card — Memory Toast deck builder & uploader

Build a flashcard deck as a local "deck directory" (`deck.json` + media), confirm the card
format with the user via samples, optionally generate images with the user's own API key,
then build + upload the ZIP pack with `scripts/upload_pack.py`.

The authoritative spec for formats, the API protocol, limits, and conflict rules is
[references/pack-format.md](references/pack-format.md) — read it before writing `deck.json`
or debugging an upload. **Converse in the user's language** even though these docs are in English.

## Prerequisites (one-time)

- **Account:** the user needs a Memory Toast account (the same one they use in the app).
- **Login (email/password):** `python3 scripts/mt_login.py` — prompts once, then stores a
  rotating refresh token in `~/.memory-toast/credentials.json` (chmod 600). The password is
  never stored. **Never ask for or handle the user's raw password yourself — the helper does.**
- **Login (Google/Facebook users):** these accounts have no password. In the app, go to
  **Settings → Copy upload token**, then run `python3 scripts/mt_login.py token` and paste it.
- `mt_login.py whoami` shows who is logged in; `mt_login.py logout` clears it.
- **Image keys (only if generating images):** the user exports their own
  `OPENAI_API_KEY` or `GEMINI_API_KEY`. You never provide a key.

## Workflow

### 1. Gather requirements

Ask (in the user's language) only what is missing, one item at a time:

- Topic and scope (e.g. "50 N3 verbs" vs. "this whole PDF").
- Deck title, description, tags, language (default `zh-TW`).
- Data source: user-provided files (PDF/images/notes) or web research.
- Whether cards should have **generated images** — and if so, the visual style
  (e.g. flat vector icon, watercolor, photoreal) and which provider (OpenAI / Gemini).

**Deck storage location (do this first — never build decks in /tmp):**

```bash
python3 scripts/mt_config.py get deck-root      # prints the saved root, or empty
```

If empty, ask the user where to keep their decks, then save it (remembered across sessions in
`~/.memory-toast/config.json`):

```bash
python3 scripts/mt_config.py set deck-root ~/Documents/MemoryToast/decks
```

Build each deck at `<deck-root>/<slug>/` — `mt_config.py path <slug>` prints the full path.

### 2. Collect data

- **User files:** read PDFs/images directly and transcribe (a dedicated `pdf` skill is
  optional, not required).
- **Web research:** use WebSearch/WebFetch. Verify factual content (dates, definitions,
  translations) against at least one more source.
- Any downloaded media for a card goes into the deck directory (e.g. `my-deck/assets/`),
  referenced by relative path in `deck.json`.

### 3. Confirm card format with examples (MANDATORY before mass production)

1. Ask the user for a front/back example of how they want cards to look (or whether to copy
   an existing deck's style).
2. Draft 2–3 sample cards from the **actual data** and show them as front/back text (plus any
   planned sections) in chat.
3. Wait for explicit approval or corrections, then apply the approved pattern to ALL cards.

Audio sections: do NOT add decorative captions (e.g. "聽發音 🔊") — the app renders a
self-explanatory play button. Use a caption only when it carries real information.

### 4. Generate images (optional — only if requested)

1. Confirm the relevant key is set: `python3 scripts/gen_image.py --provider openai --prompt x
   --out /tmp/probe.png --dry-run` (reports `key=set` or errors). If missing, tell the user the
   exact env var to export.
2. **Cost gate (MANDATORY):** before generating, tell the user *"this will call your
   `<provider>` key ~N times (~$X) — proceed?"* and wait for a yes. Image generation spends the
   user's money.
3. Generate **one image at a time**, writing into the deck's `assets/`:
   ```bash
   python3 scripts/gen_image.py --provider openai \
     --prompt "flat vector icon of a person eating, warm palette, white background" \
     --out my-deck/assets/taberu.png
   ```
   - OpenAI default model `gpt-image-1`; Gemini default `imagen-4.0-generate-001`
     (override with `--model`).
   - If one image fails (rate limit, content policy), leave that card text-only and continue;
     report which cards got no image.
4. Reference each generated PNG as a normal local image section in `deck.json`
   (`{ "kind": "image", "file": "assets/taberu.png" }`).

### 5. Build the deck directory + validate (no network)

Create the deck directory at `<deck-root>/<slug>/` (the root from step 1 — **never /tmp**,
which is wiped on reboot and loses the editable source + AI record). Put `deck.json` (schema
in pack-format.md §3) and any `assets/` there. Do NOT hand-write UUIDs, positions,
`storageRef`, or `mimeType` — the script generates them. On upload the script also writes
`.memory-toast.json` (the AI record) into this directory.

Rich text is supported: `frontContent` / `backContent` / `caption` may use an HTML subset
for font size, bold/italic/underline, color, and paragraph alignment. The script emits the
matching `*Html` keys automatically and falls back to plain text for old-style content. See
the whitelist + examples in pack-format.md §3.1 — stay inside it or tags are stripped.

Then:

```bash
python3 scripts/upload_pack.py <deck-dir> --dry-run
```

Fix any validation errors. The built ZIP lands at `<deck-dir>/build/pack.zip`.

### 6. Upload

```bash
# New deck (creates it, uploads pack v1, writes .memory-toast.json)
python3 scripts/upload_pack.py <deck-dir>

# Update the SAME deck later — just run it again. upload_pack.py reads
# .memory-toast.json and auto-updates (deckId + version); no flags needed.
python3 scripts/upload_pack.py <deck-dir>

# Override target/version explicitly if needed (--new forces a brand-new deck):
python3 scripts/upload_pack.py <deck-dir> --deck-id <id> --local-version <serverVersion>
```

Every successful upload (re)writes the deck's `.memory-toast.json` — the **AI record** a
future session reads to update or publish the deck. On a 409 conflict the script prints the
server version and the exact retry command.
**Before updating an existing deck, warn the user:** the upload replaces the server pack
wholesale; un-synced edits on the phone are overwritten on the next pull (pack-format.md §5).

If the script says the session expired, run `python3 scripts/mt_login.py` again.

### 7. Publish to the Library (optional)

Decks are private until published. To share a deck publicly, use `scripts/library_pack.py`
(reads `deckId` from `.memory-toast.json`):

```bash
# Publish once (category: language|science|history|programming|math|geography|exam|other)
python3 scripts/library_pack.py publish <deck-dir> \
  --category language --description "..." --learning-language es

# After updating the deck (upload_pack.py first), push a new public version:
python3 scripts/library_pack.py release <deck-dir> --changelog "Added digraphs"

python3 scripts/library_pack.py status          # list your published packs
```

`publish` records the `libraryPackId` back into `.memory-toast.json`, so `release` later needs
no ids. Confirm with the user before publishing — it makes the deck publicly downloadable.

### 8. Report

Tell the user: deck title, card count, media count, ZIP size, deck id, pack version, and
remind them to pull the deck in the app (open the deck → top banner "new version available" →
tap Download).
