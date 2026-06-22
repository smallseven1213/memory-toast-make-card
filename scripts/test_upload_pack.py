#!/usr/bin/env python3
"""Self-checks for the rich-text helpers + end-to-end pack build in upload_pack.py.

pytest is not installed in this repo, so these run as a plain script:

    python3 scripts/test_upload_pack.py

Every assertion mirrors the Dart whitelist contract in
apps/mobile/lib/features/cards/data/rich_text/{rich_html_parser,rich_html_serializer,rich_doc}.dart.
The same helpers live (byte-identical) in the public copy at
packages/memory-toast-make-card/scripts/upload_pack.py.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import upload_pack as up


def test_has_rich_tags():
    assert up._has_rich_tags("<b>hi</b>")
    assert up._has_rich_tags("a<br>b")
    assert up._has_rich_tags('<span style="color:red">x</span>')
    assert up._has_rich_tags("<p style='text-align:center'>x</p>")
    assert up._has_rich_tags("<strong>x</strong>")
    assert up._has_rich_tags("<EM>x</EM>")  # case-insensitive
    # No whitelist tag → false.
    assert not up._has_rich_tags("plain text")
    assert not up._has_rich_tags("")
    assert not up._has_rich_tags("2 < 3 and 5 > 4")  # stray angle brackets, no tag
    assert not up._has_rich_tags("<div>x</div>")  # not a whitelist tag
    assert not up._has_rich_tags("<script>x</script>")


def test_sanitize_keeps_whitelist():
    assert up._sanitize_rich_html("<b>bold</b>") == "<b>bold</b>"
    assert up._sanitize_rich_html("<strong>x</strong>") == "<strong>x</strong>"
    assert up._sanitize_rich_html("<i>x</i>") == "<i>x</i>"
    assert up._sanitize_rich_html("<em>x</em>") == "<em>x</em>"
    assert up._sanitize_rich_html("<u>x</u>") == "<u>x</u>"
    assert up._sanitize_rich_html("a<br>b") == "a<br>b"
    assert (
        up._sanitize_rich_html('<span style="font-size:lg;color:red">x</span>')
        == '<span style="font-size:lg;color:red">x</span>'
    )
    assert (
        up._sanitize_rich_html('<p style="text-align:center">x</p>')
        == '<p style="text-align:center">x</p>'
    )


def test_sanitize_drops_script_keeps_text():
    assert up._sanitize_rich_html("<script>alert(1)</script>") == "alert(1)"
    assert up._sanitize_rich_html("a<script>x</script>b") == "axb"


def test_sanitize_drops_div_onclick_keeps_text():
    assert up._sanitize_rich_html('<div onclick="evil()">hi</div>') == "hi"
    # Unknown tag dropped, inner whitelist tag kept.
    assert up._sanitize_rich_html("<div><b>hi</b></div>") == "<b>hi</b>"


def test_sanitize_drops_unknown_style_props():
    # font-weight is not whitelisted → dropped; font-size kept.
    assert (
        up._sanitize_rich_html('<span style="font-weight:bold;font-size:xl">x</span>')
        == '<span style="font-size:xl">x</span>'
    )
    # A span whose only style prop is disallowed → span dropped, text kept.
    assert up._sanitize_rich_html('<span style="font-weight:bold">x</span>') == "x"
    # Disallowed style VALUE (font-size:99px) → dropped.
    assert up._sanitize_rich_html('<span style="font-size:99px">x</span>') == "x"
    # Disallowed color token → dropped.
    assert up._sanitize_rich_html('<span style="color:hotpink">x</span>') == "x"
    # Allowed color token kept.
    assert (
        up._sanitize_rich_html('<span style="color:blue">x</span>')
        == '<span style="color:blue">x</span>'
    )
    # Disallowed attribute on a whitelisted tag is dropped.
    assert up._sanitize_rich_html('<b class="x" id="y">hi</b>') == "<b>hi</b>"
    # Disallowed text-align value → plain <p>.
    assert up._sanitize_rich_html('<p style="text-align:justify">x</p>') == "<p>x</p>"


def test_sanitize_entities_roundtrip():
    # Entities decoded then re-escaped canonically; stray < kept as text.
    assert up._sanitize_rich_html("a &amp; b") == "a &amp; b"
    assert up._sanitize_rich_html("<b>a &lt; b</b>") == "<b>a &lt; b</b>"
    assert up._sanitize_rich_html("2 < 3") == "2 &lt; 3"


def test_strip_tags():
    assert up._strip_tags("<b>bold</b>") == "bold"
    assert up._strip_tags('<span style="color:red">x</span>') == "x"
    assert up._strip_tags("a<br>b") == "a\nb"
    assert up._strip_tags("<p>one</p><p>two</p>") == "one\ntwo"
    assert up._strip_tags("<script>evil</script>kept") == "evilkept"
    # Entity unescape.
    assert up._strip_tags("a &amp; b") == "a & b"
    assert up._strip_tags("a &lt; b &gt; c") == "a < b > c"
    # br inside a paragraph.
    assert up._strip_tags("<p>a<br>b</p>") == "a\nb"
    # Plain text untouched.
    assert up._strip_tags("just text") == "just text"


def test_strip_tags_block_join_matches_dart():
    # Mirrors RichDoc.toPlainText(): blocks joined by '\n', no trailing newline.
    assert up._strip_tags("<p>a</p><p>b</p><p>c</p>") == "a\nb\nc"


def _build(cards):
    deck = {"title": "T", "description": "", "tags": [], "cards": cards}
    with tempfile.TemporaryDirectory() as d:
        deck_dir = Path(d)
        (deck_dir / "deck.json").write_text(json.dumps(deck))
        info = up.build_pack(deck_dir)
        import zipfile

        with zipfile.ZipFile(info["zip_path"]) as zf:
            cards_json = json.loads(zf.read("cards.json"))
        return cards_json["cards"]


def test_end_to_end_rich_card_emits_html_and_plain():
    cards = _build([
        {
            "frontContent": '<b>Bold</b> and <span style="font-size:lg;color:red">big red</span>',
            "backContent": "plain back",
        }
    ])
    c = cards[0]
    # Rich front: *Html present + sanitized, plain front is tag-stripped.
    assert c["frontContentHtml"] == (
        '<b>Bold</b> and <span style="font-size:lg;color:red">big red</span>'
    )
    assert c["frontContent"] == "Bold and big red"
    # Plain back: no html key.
    assert "backContentHtml" not in c
    assert c["backContent"] == "plain back"


def test_end_to_end_plain_card_omits_html():
    cards = _build([{"frontContent": "front", "backContent": "back"}])
    c = cards[0]
    assert "frontContentHtml" not in c
    assert "backContentHtml" not in c
    assert c["frontContent"] == "front"
    assert c["backContent"] == "back"


def test_end_to_end_explicit_html_key_wins():
    # Explicit *Html key is the rich source even when plain frontContent given.
    cards = _build([
        {
            "frontContent": "ignored plain",
            "frontContentHtml": "<i>italic</i>",
            "backContent": "back",
        }
    ])
    c = cards[0]
    assert c["frontContentHtml"] == "<i>italic</i>"
    assert c["frontContent"] == "italic"  # derived from the html, overrides input plain


def test_end_to_end_caption_html():
    import tempfile as _t

    with _t.TemporaryDirectory() as d:
        deck_dir = Path(d)
        img = deck_dir / "x.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)  # dummy png bytes
        deck = {
            "title": "T",
            "cards": [
                {
                    "frontContent": "f",
                    "backContent": "b",
                    "frontSections": [
                        {"kind": "image", "file": "x.png", "caption": "<b>cap</b>"}
                    ],
                }
            ],
        }
        (deck_dir / "deck.json").write_text(json.dumps(deck))
        info = up.build_pack(deck_dir)
        import zipfile

        with zipfile.ZipFile(info["zip_path"]) as zf:
            cards = json.loads(zf.read("cards.json"))["cards"]
        sec = cards[0]["frontSections"][0]
        assert sec["captionHtml"] == "<b>cap</b>"
        assert sec["caption"] == "cap"


def test_end_to_end_plain_caption_omits_html():
    import tempfile as _t

    with _t.TemporaryDirectory() as d:
        deck_dir = Path(d)
        img = deck_dir / "x.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        deck = {
            "title": "T",
            "cards": [
                {
                    "frontContent": "f",
                    "backContent": "b",
                    "frontSections": [
                        {"kind": "image", "file": "x.png", "caption": "plain cap"}
                    ],
                }
            ],
        }
        (deck_dir / "deck.json").write_text(json.dumps(deck))
        info = up.build_pack(deck_dir)
        import zipfile

        with zipfile.ZipFile(info["zip_path"]) as zf:
            cards = json.loads(zf.read("cards.json"))["cards"]
        sec = cards[0]["frontSections"][0]
        assert "captionHtml" not in sec
        assert sec["caption"] == "plain cap"


def test_empty_check_uses_plain_text():
    # A card whose front is rich markup that strips to empty must fail validation.
    deck = {"title": "T", "cards": [{"frontContent": "<b></b>", "backContent": "b"}]}
    with tempfile.TemporaryDirectory() as d:
        deck_dir = Path(d)
        (deck_dir / "deck.json").write_text(json.dumps(deck))
        try:
            up.build_pack(deck_dir)
        except SystemExit:
            pass  # fail() calls sys.exit(1) — expected
        else:
            raise AssertionError("expected validation failure for empty stripped front")


# ---------------------------------------------------------------------------
# Stable card ids across rebuilds — so updating a deck preserves the user's
# study progress. The app keys study progress by card id and prunes orphans on
# pull, so a re-upload that re-mints ids would wipe progress. Mirrors the Dart
# content match in features/decks/data/services/progress_remap.dart.
# ---------------------------------------------------------------------------

def _build_in(deck_dir, cards):
    """Build a pack inside a PERSISTENT dir, so a second build can reuse ids
    from the first build's pack.zip. Returns the built cards.json list."""
    deck = {"title": "T", "description": "", "tags": [], "cards": cards}
    (deck_dir / "deck.json").write_text(json.dumps(deck))
    info = up.build_pack(deck_dir)
    import zipfile

    with zipfile.ZipFile(info["zip_path"]) as zf:
        return json.loads(zf.read("cards.json"))["cards"]


def test_assign_stable_ids_reuses_unchanged_card_ids():
    prior = [
        {"id": "id-A", "frontContent": "apple", "backContent": "def-A"},
        {"id": "id-B", "frontContent": "banana", "backContent": "def-B"},
    ]
    new = [
        {"frontContent": "banana", "backContent": "def-B"},
        {"frontContent": "apple", "backContent": "def-A"},
    ]
    up.assign_stable_ids(new, prior)
    assert new[0]["id"] == "id-B"  # matched by content, order-independent
    assert new[1]["id"] == "id-A"


def test_assign_stable_ids_back_edit_keeps_id():
    prior = [{"id": "id-A", "frontContent": "apple", "backContent": "one"}]
    new = [{"frontContent": "apple", "backContent": "one; two"}]
    up.assign_stable_ids(new, prior)
    assert new[0]["id"] == "id-A"  # front-stable match survives a back edit


def test_assign_stable_ids_new_card_gets_fresh_id():
    prior = [{"id": "id-A", "frontContent": "apple", "backContent": "def-A"}]
    new = [
        {"frontContent": "apple", "backContent": "def-A"},
        {"frontContent": "cherry", "backContent": "def-C"},
    ]
    up.assign_stable_ids(new, prior)
    assert new[0]["id"] == "id-A"
    assert new[1]["id"] and new[1]["id"] != "id-A"
    assert len(new[1]["id"]) >= 32  # a real uuid, not a placeholder


def test_assign_stable_ids_front_change_gets_fresh_id():
    prior = [{"id": "id-A", "frontContent": "apple", "backContent": "same"}]
    new = [{"frontContent": "orange", "backContent": "same"}]
    up.assign_stable_ids(new, prior)
    assert new[0]["id"] != "id-A"


def test_assign_stable_ids_duplicate_front_disambiguated_by_back():
    prior = [
        {"id": "id-1", "frontContent": "dup", "backContent": "b1"},
        {"id": "id-2", "frontContent": "dup", "backContent": "b2"},
    ]
    new = [
        {"frontContent": "dup", "backContent": "b2"},
        {"frontContent": "dup", "backContent": "b1"},
    ]
    up.assign_stable_ids(new, prior)
    assert new[0]["id"] == "id-2"
    assert new[1]["id"] == "id-1"


def test_build_pack_keeps_card_ids_stable_across_rebuilds():
    with tempfile.TemporaryDirectory() as d:
        deck_dir = Path(d)
        v1 = _build_in(deck_dir, [
            {"frontContent": "apple", "backContent": "a fruit"},
            {"frontContent": "banana", "backContent": "yellow"},
        ])
        ids_v1 = {c["frontContent"]: c["id"] for c in v1}

        # Rebuild: edit banana's back, append cherry; apple untouched.
        v2 = _build_in(deck_dir, [
            {"frontContent": "apple", "backContent": "a fruit"},
            {"frontContent": "banana", "backContent": "yellow; a fruit"},
            {"frontContent": "cherry", "backContent": "red"},
        ])
        ids_v2 = {c["frontContent"]: c["id"] for c in v2}

        assert ids_v2["apple"] == ids_v1["apple"], "unchanged card kept its id"
        assert ids_v2["banana"] == ids_v1["banana"], "back-edit kept the id"
        assert ids_v2["cherry"] not in ids_v1.values(), "new card got a fresh id"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} tests passed")


if __name__ == "__main__":
    main()
