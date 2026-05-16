"""Dispatch + alias + cost lookup. Pure stdlib — runs in CI without
ffmpeg/libreoffice/calibre."""
import pytest

from freeconvert import (
    COST_BY_CATEGORY,
    HANDLERS,
    UnsupportedConversionError,
    convert,
    lookup,
    supported_pairs,
)
from freeconvert.handlers import _norm


def test_aliases_normalize():
    assert _norm("JPEG") == "jpg"
    assert _norm(".PDF") == "pdf"
    assert _norm("word") == "docx"
    assert _norm("excel") == "xlsx"
    assert _norm("powerpoint") == "pptx"
    assert _norm("markdown") == "md"


def test_lookup_resolves_aliases():
    h1 = lookup("jpeg", "png")
    h2 = lookup("jpg", "png")
    assert h1 is not None
    assert h2 is not None
    assert h1[1] == "image" == h2[1]
    # Handler function should be the same instance for both.
    assert h1[0] is h2[0]


def test_lookup_returns_none_for_unsupported():
    assert lookup("zip", "pdf") is None
    assert lookup("nonexistent", "alsonope") is None


def test_supported_pairs_includes_key_conversions():
    pairs = set(supported_pairs())
    for p in [("pdf", "docx"), ("docx", "pdf"), ("csv", "json"),
              ("srt", "vtt"), ("png", "jpg"), ("mp3", "wav"),
              ("mp4", "gif"), ("epub", "mobi")]:
        assert p in pairs, f"expected {p} in supported_pairs()"


def test_cost_table_covers_every_category():
    """Every category referenced in HANDLERS must have a cost entry."""
    categories = {cat for (_, cat) in HANDLERS.values()}
    missing = categories - set(COST_BY_CATEGORY.keys())
    assert not missing, f"categories missing from COST_BY_CATEGORY: {missing}"


def test_unsupported_raises(tmp_path):
    src = tmp_path / "in.zip"
    src.write_bytes(b"PK\x03\x04")  # not a real zip but file must exist
    with pytest.raises(UnsupportedConversionError):
        convert(str(src), str(tmp_path / "out.pdf"))


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        convert(str(tmp_path / "nope.png"), str(tmp_path / "out.jpg"))


def test_infer_formats_from_extensions():
    # convert() should pick the right handler based on extensions alone.
    # We use the lookup path (not actual execution) here to keep this test
    # stdlib-only.
    resolved = lookup("pdf", "docx")
    assert resolved is not None
    _, category, cost, src, dst = resolved
    assert category == "pdf_to_word"
    assert cost == 1000
    assert (src, dst) == ("pdf", "docx")
