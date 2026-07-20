from pathlib import Path

import pytest

from yt_live_downloader.filesystem import (
    artifact_filename,
    resolve_stored_path,
    safe_title_component,
    unique_destination,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("  A useful title  ", "A useful title"),
        ("AC/DC: live?", "AC – DC – live"),
        ("../\\", "Untitled"),
        ("NUL", "NUL_"),
        ("\x00\n", "Untitled"),
    ],
)
def test_safe_title_component(title, expected):
    assert safe_title_component(title) == expected


def test_artifact_filename_has_stable_identifier():
    assert (
        artifact_filename("A stream", ".MP4", source_id="abc-123", job_id="fallback")
        == "A stream [abc-123].mp4"
    )


def test_unicode_artifact_filename_can_be_created(tmp_path):
    title = "日本語 Café 👩🏽\N{ZERO WIDTH JOINER}💻"
    filename = artifact_filename(title, "mp4", source_id="abc123", job_id="fallback")
    artifact = tmp_path / filename

    artifact.write_bytes(b"media")

    assert artifact.name == f"{title} [abc123].mp4"
    assert artifact.read_bytes() == b"media"


def test_japanese_title_is_preserved():
    assert safe_title_component("日本語のライブ配信") == "日本語のライブ配信"


def test_combining_characters_are_normalized_without_being_lost():
    assert safe_title_component("Cafe\N{COMBINING ACUTE ACCENT}") == "Café"


def test_contextual_script_joiner_is_preserved():
    title = "می\N{ZERO WIDTH NON-JOINER}خواهم"
    assert safe_title_component(title) == title


@pytest.mark.parametrize(
    "emoji",
    [
        "👩🏽\N{ZERO WIDTH JOINER}💻",
        "👨\N{ZERO WIDTH JOINER}👩\N{ZERO WIDTH JOINER}👧\N{ZERO WIDTH JOINER}👦",
        "❤\N{VARIATION SELECTOR-16}\N{ZERO WIDTH JOINER}🔥",
    ],
)
def test_composite_emoji_joiners_are_preserved(emoji):
    assert safe_title_component(f"Stream {emoji}") == f"Stream {emoji}"


@pytest.mark.parametrize(
    "symbol",
    [
        "✈\N{VARIATION SELECTOR-15}",
        "✈\N{VARIATION SELECTOR-16}",
    ],
)
def test_emoji_variation_selectors_are_preserved(symbol):
    assert safe_title_component(symbol) == symbol


def test_dangerous_bidi_and_invisible_controls_are_removed():
    title = (
        "safe\N{RIGHT-TO-LEFT OVERRIDE}gpj.exe\N{POP DIRECTIONAL FORMATTING}"
        "\N{LEFT-TO-RIGHT ISOLATE}isolated\N{POP DIRECTIONAL ISOLATE}"
        "\N{LEFT-TO-RIGHT MARK}\N{ARABIC LETTER MARK}"
        "\N{ZERO WIDTH SPACE}\N{WORD JOINER}\N{SOFT HYPHEN}"
        "\N{ZERO WIDTH NO-BREAK SPACE}"
    )
    assert safe_title_component(title) == "safegpj.exeisolated"


def test_lone_surrogates_are_removed_before_utf8_encoding():
    assert safe_title_component("safe\ud800title") == "safetitle"


@pytest.mark.parametrize(
    "join_control",
    [
        "\N{ZERO WIDTH NON-JOINER}",
        "\N{ZERO WIDTH JOINER}",
    ],
)
def test_dangling_join_controls_do_not_create_invisible_names(join_control):
    assert safe_title_component(join_control) == "Untitled"
    assert safe_title_component(f"Stream{join_control}", max_length=7) == "Stream"


def test_filename_caps_multibyte_titles_by_encoded_size():
    title = "日本語ライブ👩🏽\N{ZERO WIDTH JOINER}💻" * 100
    component = safe_title_component(title)
    filename = artifact_filename(
        component, "mp4", source_id="abc123", job_id="fallback"
    )
    assert title.startswith(component)
    assert len(component.encode("utf-8")) <= 160
    assert len(filename.encode("utf-8")) <= 255


def test_unique_destination_never_overwrites(tmp_path):
    first = tmp_path / "Stream [abc].mp4"
    first.write_bytes(b"first")
    assert unique_destination(tmp_path, first.name).name == "Stream [abc] (2).mp4"


def test_resolve_stored_path_rejects_escape(tmp_path):
    with pytest.raises(ValueError):
        resolve_stored_path(tmp_path, "../outside.mp4")


def test_resolve_stored_path_accepts_child(tmp_path):
    expected = Path(tmp_path, "video.mp4").resolve()
    assert resolve_stored_path(tmp_path, "video.mp4") == expected
