# memory-toast-make-card

[![npm](https://img.shields.io/npm/v/memory-toast-make-card)](https://www.npmjs.com/package/memory-toast-make-card)
[![license: MIT](https://img.shields.io/npm/l/memory-toast-make-card)](LICENSE)

A [Claude Code](https://claude.com/claude-code) skill that turns your study material —
requirements, web research, PDFs, images, notes — into [Memory Toast](https://memory-toast-api.smallseven-87b.workers.dev)
flashcard decks (卡包), optionally **generates card images with your own OpenAI/Gemini key**,
and uploads the deck to **your** Memory Toast account as a ZIP pack.

It is the public, token-authenticated counterpart of the internal `make-card` skill: you sign
in once with your own account, and your password is never stored — only a rotating refresh token.

## Install

### Option A — npm

```bash
# Into your user skills (~/.claude/skills/)
npx memory-toast-make-card install

# Or into the current project (./.claude/skills/)
npx memory-toast-make-card install --project
```

### Option B — skills.sh

Install from the registry, or copy the folder into `~/.claude/skills/memory-toast-make-card/`
manually. The skill is the directory containing `SKILL.md`.

## Prerequisites

1. **A Memory Toast account** — the same one you use in the app.
2. **Log in once** (stores a refresh token in `~/.memory-toast/credentials.json`, chmod 600;
   no secret is written to disk except the rotating token):
   ```bash
   # Email/password account:
   python3 ~/.claude/skills/memory-toast-make-card/scripts/mt_login.py

   # Google/Facebook account (no password): copy the token from the app
   # (Settings → Copy upload token), then paste it here:
   python3 ~/.claude/skills/memory-toast-make-card/scripts/mt_login.py token
   ```
   `mt_login.py whoami` shows the logged-in account; `mt_login.py logout` removes the token.
3. **(Optional) image generation** — export your own key for the provider you want:
   ```bash
   export OPENAI_API_KEY=sk-...      # OpenAI gpt-image-1
   export GEMINI_API_KEY=...         # Google Imagen / Gemini
   ```
   You always supply your own key; the skill never provides one. Image generation calls a
   **paid** API — the skill confirms the cost with you before generating in bulk.

## Usage

In Claude Code, just ask — for example:

> Make a Memory Toast deck of 30 common Japanese N3 verbs, each with a flat-vector icon image.

The skill will gather requirements, draft a few sample cards for your approval, generate any
images (with your confirmation on cost), build the ZIP, and upload it. Then open the deck in
the Memory Toast app and tap **Download** on the "new version available" banner.

You can also drive the scripts directly:

```bash
S=~/.claude/skills/memory-toast-make-card/scripts

# One-time: choose where decks are kept (remembered; keeps them out of /tmp)
python3 $S/mt_config.py set deck-root ~/Documents/MemoryToast/decks

# Generate an image with your own key
python3 $S/gen_image.py --provider openai \
  --prompt "flat vector icon of a person eating, warm palette, white background" \
  --out my-deck/assets/taberu.png

# Validate a deck directory offline (builds my-deck/build/pack.zip)
python3 $S/upload_pack.py my-deck --dry-run

# Create a new deck and upload (writes my-deck/.memory-toast.json)
python3 $S/upload_pack.py my-deck

# Update the SAME deck later — just run it again; it auto-reads the record
python3 $S/upload_pack.py my-deck

# Publish to the public Library, then release new versions later
python3 $S/library_pack.py publish my-deck --category language --description "..."
python3 $S/library_pack.py release my-deck --changelog "Added 10 cards"
```

See [`references/pack-format.md`](references/pack-format.md) for the `deck.json` schema, the
upload protocol, limits, and version/conflict rules.

### Deck storage, updates & publishing

- Decks are built under a **remembered root** (`mt_config.py set deck-root <path>`), not /tmp,
  so the editable source survives reboots. Each deck folder gets a `.memory-toast.json` record
  (deckId, version, structure, …) on upload — the state a later session reads to continue.
- To **update** a deck, edit it and re-run `upload_pack.py <deck-dir>`; it reads that record and
  bumps the server version automatically — no ids to remember (`--new` forces a fresh deck).
- To **share** a deck, `library_pack.py publish` it to the public Library (categories: language,
  science, history, programming, math, geography, exam, other), then `release` new versions as
  you update it. `status` lists what you've published.

## Rich text in cards

A card's `frontContent` / `backContent` (and a section's `caption`) may contain a small **HTML
subset** for formatting; plain text is always valid, so existing decks keep working unchanged.
`make-card` emits this HTML straight from `deck.json` — what you write is what the app renders.

Allowed tags:

- `<b>` / `<strong>` — bold
- `<i>` / `<em>` — italic
- `<u>` — underline
- `<br>` — line break
- `<span style="font-size:sm|base|lg|xl;color:<token>">` — inline size and/or color
- `<p style="text-align:left|center|right">…</p>` — paragraph alignment

Font sizes are four semantic levels — `sm`, `base`, `lg`, `xl` (not arbitrary px). Color tokens
are: `primary`, `red`, `orange`, `green`, `blue`, `purple`, `gray`.

```json
{ "frontContent": "<b>紅蘋果</b>", "backContent": "apple <span style=\"font-size:lg;color:red\">(fruit)</span>" }
```

Anything outside the whitelist (other tags, attributes, styles, or color values) is dropped
while its text is kept — no scripts ever run. See
[`references/pack-format.md`](references/pack-format.md) §3.1 for the full rules.

## Configuration

- **Server URL** resolves as: `--api` flag → `MEMORY_TOAST_API_URL` env → stored value →
  built-in default. Self-hosters can point the skill at their own server without editing files.
- **Image model** is overridable with `--model`. Defaults: `gpt-image-1` (OpenAI),
  `imagen-4.0-generate-001` (Gemini). Pass a `gemini-*-image` model (e.g.
  `gemini-2.5-flash-image`) to use the `generateContent` path instead of Imagen `predict`.
  > Note: Google's older image-generation model endpoints are deprecated after **2026-06-30** —
  > switch the default via `--model` if generation starts failing.

## Security notes

- The password is read interactively (never echoed) and **never stored** — only the rotating
  7-day refresh token, in a `chmod 600` file under your home directory. `logout` deletes it.
- API keys and tokens are never printed; error paths redact them.
- These are **stateless JWTs**: a leaked refresh token cannot be individually revoked before
  its 7-day expiry. Keep the credential file private and run `logout` on shared machines.
- An upload **replaces** the deck's server pack wholesale — push any un-synced phone edits
  first (see pack-format.md §5).

## Requirements

- Python 3.9+ (standard library only — no `pip install`).
- Node 16+ (for the `npx` installer only).

## License

MIT — see [LICENSE](LICENSE). (Copyright holder is set to `smallseven`; edit it if you'd
rather publish under a different name/org.)
