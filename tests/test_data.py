"""CSV ↔ JSON. Stdlib-only handlers."""
import json

from freeconvert import convert


def test_csv_to_json(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("name,age\nAda,32\nGrace,41\n", encoding="utf-8")
    out = tmp_path / "out.json"
    convert(str(src), str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == [{"name": "Ada", "age": "32"}, {"name": "Grace", "age": "41"}]


def test_json_to_csv_array(tmp_path):
    src = tmp_path / "in.json"
    src.write_text(json.dumps([{"a": 1, "b": 2}, {"a": 3, "b": 4}]), encoding="utf-8")
    out = tmp_path / "out.csv"
    convert(str(src), str(out))
    body = out.read_text(encoding="utf-8")
    # Header may be in either order — openpyxl preserves dict-iteration order
    # which is insertion order, but our handler uses set-of-keys + list().
    assert "a" in body and "b" in body
    assert "1" in body and "4" in body


def test_json_to_csv_single_object_wraps(tmp_path):
    src = tmp_path / "in.json"
    src.write_text(json.dumps({"k": "v", "n": 7}), encoding="utf-8")
    out = tmp_path / "out.csv"
    convert(str(src), str(out))
    body = out.read_text(encoding="utf-8")
    assert "k" in body and "n" in body
    assert "v" in body and "7" in body


def test_xlsx_to_csv_roundtrip(tmp_path):
    # Requires openpyxl — installed via [dev]
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["a", "b"])
    ws.append([1, 2])
    ws.append([3, 4])
    src = tmp_path / "in.xlsx"
    wb.save(str(src))

    out = tmp_path / "out.csv"
    convert(str(src), str(out))
    body = out.read_text(encoding="utf-8")
    assert "a,b" in body or "a\tb" in body
    assert "1,2" in body or "1\t2" in body
