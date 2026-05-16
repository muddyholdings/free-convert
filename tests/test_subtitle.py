"""SRT ↔ VTT ↔ TXT — handlers are stdlib-only, safe to run in CI without
external tools."""
from freeconvert import convert


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:04,000
Hello, world.

2
00:00:05,200 --> 00:00:07,800
Subtitles work.
"""


def test_srt_to_vtt(tmp_path):
    src = tmp_path / "in.srt"
    src.write_text(SAMPLE_SRT, encoding="utf-8")
    out = tmp_path / "out.vtt"
    convert(str(src), str(out))
    body = out.read_text(encoding="utf-8")
    assert body.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:04.000" in body
    assert "Hello, world." in body


def test_vtt_to_srt(tmp_path):
    src = tmp_path / "in.vtt"
    src.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "Hello, world.\n\n"
        "00:00:05.200 --> 00:00:07.800\n"
        "Subtitles work.\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.srt"
    convert(str(src), str(out))
    body = out.read_text(encoding="utf-8")
    assert "1" in body
    assert "00:00:01,000 --> 00:00:04,000" in body
    assert "Hello, world." in body


def test_srt_to_txt_strips_timestamps(tmp_path):
    src = tmp_path / "in.srt"
    src.write_text(SAMPLE_SRT, encoding="utf-8")
    out = tmp_path / "out.txt"
    convert(str(src), str(out))
    body = out.read_text(encoding="utf-8")
    assert "Hello, world." in body
    assert "Subtitles work." in body
    assert "-->" not in body
    assert "00:00:01" not in body
