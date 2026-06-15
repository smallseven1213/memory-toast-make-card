#!/usr/bin/env python3
"""Persistent config for make-card — remembered across sessions.

Stored at ~/.memory-toast/config.json (chmod 600), shared by every make-card
script. Zero deps — Python 3.9+ stdlib only.

The main use is `deckRoot`: the folder where deck directories live so they are
NOT built in /tmp (which is wiped on reboot). Each deck is a subfolder
`<deckRoot>/<slug>/` holding deck.json, assets, build/, and .memory-toast.json.

CLI:
  mt_config.py show                       # print the whole config
  mt_config.py get deck-root              # print deckRoot (empty line if unset)
  mt_config.py set deck-root ~/Decks      # set + persist deckRoot (expands ~)
  mt_config.py path <slug>                # print <deckRoot>/<slug> (errors if unset)
"""

import argparse
import json
import os
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".memory-toast"
CONFIG_PATH = CONFIG_DIR / "config.json"

# CLI keys (kebab) -> JSON keys (camel)
_KEY_MAP = {"deck-root": "deckRoot"}


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text() or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def get_deck_root():
    """Return the configured deck root as a string, or None if unset."""
    return load_config().get("deckRoot")


def set_deck_root(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    cfg = load_config()
    cfg["deckRoot"] = str(resolved)
    save_config(cfg)
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    g = sub.add_parser("get")
    g.add_argument("key", choices=list(_KEY_MAP))
    s = sub.add_parser("set")
    s.add_argument("key", choices=list(_KEY_MAP))
    s.add_argument("value")
    p = sub.add_parser("path")
    p.add_argument("slug")
    args = parser.parse_args()

    if args.cmd == "show":
        print(json.dumps(load_config(), ensure_ascii=False, indent=2))
    elif args.cmd == "get":
        print(load_config().get(_KEY_MAP[args.key], ""))
    elif args.cmd == "set":
        if args.key == "deck-root":
            resolved = set_deck_root(args.value)
            print(f"deckRoot = {resolved}")
    elif args.cmd == "path":
        root = get_deck_root()
        if not root:
            print("ERROR: deckRoot is unset — run: mt_config.py set deck-root <path>",
                  file=sys.stderr)
            sys.exit(1)
        print(str(Path(root) / args.slug))


if __name__ == "__main__":
    main()
