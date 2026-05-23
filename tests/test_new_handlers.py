"""Tests for the v0.2.0 expansion: ass/sbv subtitle, data (csv/tsv/json/xml/yaml/
md/sql), archive zip↔tgz, mesh stl→obj. Skipped when the optional dep group
isn't installed."""
import json
import pytest

from freeconvert import convert


# --- Subtitle expansion -----------------------------------------------------

SAMPLE_SRT = """1
00:00:01,000 --> 00:00:03,500
Hello world

2
00:00:04,000 --> 00:00:06,000
Test subtitle
"""


def test_srt_to_ass(tmp_path):
    src = tmp_path / "in.srt"; src.write_text(SAMPLE_SRT)
    out = tmp_path / "out.ass"
    convert(str(src), str(out))
    body = out.read_text()
    assert "[Script Info]" in body
    assert "[Events]" in body
    assert "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Hello world" in body
    assert "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,Test subtitle" in body


def test_ass_to_srt(tmp_path):
    src = tmp_path / "in.ass"
    src.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n"
        "[V4+ Styles]\nFormat: Name\nStyle: Default\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,Hello world\n"
        "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,Test subtitle\n"
    )
    out = tmp_path / "out.srt"
    convert(str(src), str(out))
    body = out.read_text()
    assert "1\n00:00:01,000 --> 00:00:03,500\nHello world" in body


def test_srt_to_sbv(tmp_path):
    src = tmp_path / "in.srt"; src.write_text(SAMPLE_SRT)
    out = tmp_path / "out.sbv"
    convert(str(src), str(out))
    body = out.read_text()
    assert "00:00:01.000,00:00:03.500" in body
    assert "Hello world" in body


def test_sbv_to_srt(tmp_path):
    src = tmp_path / "in.sbv"
    src.write_text("00:00:01.000,00:00:03.500\nHello\n\n00:00:04.000,00:00:06.000\nWorld\n")
    out = tmp_path / "out.srt"
    convert(str(src), str(out))
    body = out.read_text()
    assert "1\n00:00:01,000 --> 00:00:03,500" in body
    assert "Hello" in body


# --- Data expansion (pandas-backed; skip if pandas missing) -----------------

pd = pytest.importorskip("pandas")


def test_csv_to_tsv(tmp_path):
    src = tmp_path / "in.csv"; src.write_text("name,age\nalice,30\nbob,25\n")
    out = tmp_path / "out.tsv"
    convert(str(src), str(out))
    body = out.read_text()
    assert "name\tage" in body
    assert "alice\t30" in body


def test_json_to_yaml(tmp_path):
    pytest.importorskip("yaml")
    src = tmp_path / "in.json"
    src.write_text(json.dumps([{"name": "alice", "age": 30}, {"name": "bob", "age": 25}]))
    out = tmp_path / "out.yaml"
    convert(str(src), str(out))
    body = out.read_text()
    assert "- name: alice" in body
    assert "age: 30" in body


def test_yaml_to_json(tmp_path):
    pytest.importorskip("yaml")
    src = tmp_path / "in.yaml"
    src.write_text("- name: alice\n  age: 30\n- name: bob\n  age: 25\n")
    out = tmp_path / "out.json"
    convert(str(src), str(out))
    data = json.loads(out.read_text())
    assert data == [{"name": "alice", "age": 30}, {"name": "bob", "age": 25}]


def test_csv_to_xml(tmp_path):
    pytest.importorskip("lxml")
    src = tmp_path / "in.csv"; src.write_text("name,age\nalice,30\n")
    out = tmp_path / "out.xml"
    convert(str(src), str(out))
    body = out.read_text()
    assert "<?xml" in body
    assert "<name>alice</name>" in body
    assert "<age>30</age>" in body


def test_csv_to_markdown(tmp_path):
    pytest.importorskip("tabulate")
    src = tmp_path / "in.csv"; src.write_text("name,age\nalice,30\nbob,25\n")
    out = tmp_path / "out.md"
    convert(str(src), str(out))
    body = out.read_text()
    # pandas.to_markdown formats with | dividers
    assert "| name" in body
    assert "alice" in body


def test_csv_to_sql(tmp_path):
    src = tmp_path / "users.csv"; src.write_text("name,age\nalice,30\n")
    out = tmp_path / "out.sql"
    convert(str(src), str(out))
    body = out.read_text()
    assert 'CREATE TABLE "users"' in body
    assert "INSERT INTO" in body
    assert "'alice'" in body


# --- Archive expansion ------------------------------------------------------

def test_zip_to_tgz(tmp_path):
    import zipfile
    src = tmp_path / "in.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("a.txt", "hello")
        z.writestr("b.txt", "world")
    out = tmp_path / "out.tgz"
    convert(str(src), str(out))
    assert out.exists() and out.stat().st_size > 0
    import tarfile
    with tarfile.open(out, "r:gz") as t:
        names = {os.path.basename(n) for n in t.getnames()}
    assert "a.txt" in names and "b.txt" in names


def test_tgz_to_zip(tmp_path):
    import tarfile, zipfile
    src = tmp_path / "in.tgz"
    # Build a small .tgz
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("hello")
    (tmp_path / "src" / "b.txt").write_text("world")
    with tarfile.open(src, "w:gz") as t:
        t.add(str(tmp_path / "src"), arcname=".")
    out = tmp_path / "out.zip"
    convert(str(src), str(out))
    assert out.exists() and out.stat().st_size > 0
    with zipfile.ZipFile(out) as z:
        names = {n.split("/")[-1] for n in z.namelist()}
    assert "a.txt" in names and "b.txt" in names


# --- Mesh expansion (skip if trimesh missing) -------------------------------

trimesh = pytest.importorskip("trimesh")
import os  # noqa: E402


def test_stl_to_obj(tmp_path):
    import struct
    # Tiny binary STL (2 triangles)
    src = tmp_path / "cube.stl"
    faces = [((0,0,-1), [(0,0,0),(1,1,0),(1,0,0)]),
             ((0,0,-1), [(0,0,0),(0,1,0),(1,1,0)])]
    with open(src, "wb") as f:
        f.write(b"binary stl test" + b" "*(80-15))
        f.write(struct.pack("<I", len(faces)))
        for normal, verts in faces:
            for x in normal: f.write(struct.pack("<f", x))
            for v in verts:
                for x in v: f.write(struct.pack("<f", x))
            f.write(b"\x00\x00")
    out = tmp_path / "out.obj"
    convert(str(src), str(out))
    body = out.read_text()
    assert "v " in body  # vertex lines
    assert "f " in body  # face lines


def test_obj_to_stl(tmp_path):
    src = tmp_path / "tri.obj"
    src.write_text(
        "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
        "f 1 2 3\n"
    )
    out = tmp_path / "out.stl"
    convert(str(src), str(out))
    assert out.exists() and out.stat().st_size > 0
