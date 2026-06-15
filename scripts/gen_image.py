#!/usr/bin/env python3
"""Generate one image for a flashcard using the user's OWN OpenAI or Gemini API key.

Zero third-party deps — Python 3.9+ stdlib only. The API key is read from the
environment (or the credential store) and is never printed or stored by this script.

Usage:
  gen_image.py --provider openai|gemini --prompt "..." --out PATH
               [--size WxH] [--model ID] [--dry-run]

API key (env var, or "imageKeys" in ~/.memory-toast/credentials.json):
  openai → OPENAI_API_KEY
  gemini → GEMINI_API_KEY

Defaults:
  openai → gpt-image-1              (sizes: 1024x1024, 1536x1024, 1024x1536, auto)
  gemini → imagen-4.0-generate-001  (Imagen "predict" path; pass a gemini-*-image
           model, e.g. gemini-2.5-flash-image, to use the "generateContent" path)

WARNING: this calls a PAID API billed to the user's key. The skill must confirm the
cost with the user before generating images in bulk. Output is written as raw bytes
(PNG by default) to --out.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "memory-toast-make-card/1.0"
GEMINI_API_VERSION = "v1beta"
CRED_PATH = Path.home() / ".memory-toast" / "credentials.json"

ENV_VAR = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
DEFAULT_MODEL = {"openai": "gpt-image-1", "gemini": "imagen-4.0-generate-001"}


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def resolve_key(provider: str) -> str:
    """Env var first, then imageKeys.<provider> in the credential store."""
    key = os.environ.get(ENV_VAR[provider])
    if key:
        return key
    try:
        if CRED_PATH.is_file():
            creds = json.loads(CRED_PATH.read_text())
            return (creds.get("imageKeys") or {}).get(provider)
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _http_post(url: str, payload: dict, headers: dict, timeout: int = 180):
    data = json.dumps(payload).encode()
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, {"raw": text}


def _http_get_bytes(url: str, timeout: int = 120) -> bytes:
    """Download raw bytes (used for DALL·E URL responses)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _size_to_aspect(size: str):
    """Map a WxH size to the closest Imagen aspectRatio bucket (or None)."""
    if not size or "x" not in size.lower():
        return None
    try:
        w, h = (int(x) for x in size.lower().split("x"))
    except ValueError:
        return None
    if w == h:
        return "1:1"
    return "4:3" if w > h else "3:4"


def gen_openai(key: str, model: str, prompt: str, size: str) -> bytes:
    # No response_format param: gpt-image-1 returns base64 (and rejects the param),
    # while DALL·E returns a URL — handle whichever comes back.
    payload = {"model": model, "prompt": prompt, "n": 1, "size": size}
    status, res = _http_post("https://api.openai.com/v1/images/generations",
                             payload, {"Authorization": f"Bearer {key}"})
    if status != 200:
        fail(f"OpenAI image API error ({status}): {(res.get('error') or {}).get('message') or res}")
    data = res.get("data") or []
    if data and data[0].get("b64_json"):
        return base64.b64decode(data[0]["b64_json"])
    if data and data[0].get("url"):
        return _http_get_bytes(data[0]["url"])
    fail(f"OpenAI returned no image data: {res}")


def gen_gemini_imagen(key: str, model: str, prompt: str, size: str) -> bytes:
    url = f"https://generativelanguage.googleapis.com/{GEMINI_API_VERSION}/models/{model}:predict"
    params = {"sampleCount": 1}
    aspect = _size_to_aspect(size)
    if aspect:
        params["aspectRatio"] = aspect
    payload = {"instances": [{"prompt": prompt}], "parameters": params}
    status, res = _http_post(url, payload, {"x-goog-api-key": key})
    if status != 200:
        fail(f"Gemini Imagen API error ({status}): {(res.get('error') or {}).get('message') or res}")
    preds = res.get("predictions") or []
    for p in preds:
        if p.get("bytesBase64Encoded"):
            return base64.b64decode(p["bytesBase64Encoded"])
    reasons = [p.get("raiFilteredReason") for p in preds if p.get("raiFilteredReason")]
    fail("Gemini Imagen returned no image"
         + (f" (filtered: {reasons})" if reasons else f": {res}"))


def gen_gemini_content(key: str, model: str, prompt: str) -> bytes:
    url = f"https://generativelanguage.googleapis.com/{GEMINI_API_VERSION}/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    status, res = _http_post(url, payload, {"x-goog-api-key": key})
    if status != 200:
        fail(f"Gemini API error ({status}): {(res.get('error') or {}).get('message') or res}")
    for cand in res.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                return base64.b64decode(inline["data"])
    finish = (res.get("candidates") or [{}])[0].get("finishReason")
    fail(f"Gemini returned no image (finishReason={finish}): {res}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", required=True, choices=["openai", "gemini"])
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--size", default="1024x1024",
                        help="OpenAI: literal size; Gemini Imagen: mapped to aspectRatio")
    parser.add_argument("--model", help="override the provider's default model")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate args + key presence without calling the API (no cost)")
    args = parser.parse_args()

    model = args.model or DEFAULT_MODEL[args.provider]
    key = resolve_key(args.provider)
    if not key:
        fail(f"missing API key — set {ENV_VAR[args.provider]} in your environment, "
             f'or add "imageKeys": {{"{args.provider}": "..."}} to {CRED_PATH}')

    if args.dry_run:
        print(f"[dry-run] provider={args.provider} model={model} size={args.size} "
              f"out={args.out} key=set — no API call made (no cost).")
        return

    if args.provider == "openai":
        img = gen_openai(key, model, args.prompt, args.size)
    elif model.startswith("imagen"):
        img = gen_gemini_imagen(key, model, args.prompt, args.size)
    else:
        img = gen_gemini_content(key, model, args.prompt)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(img)
    print(f"Wrote {args.out} ({len(img):,} bytes) via {args.provider}/{model}")


if __name__ == "__main__":
    main()
