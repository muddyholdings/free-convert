"""Universal file format converter — a single dispatch table mapping
(src_ext, dst_ext) → handler function.

Each handler is a thin wrapper around a battle-tested external tool:
- **Pillow** for image ↔ image and image → PDF
- **cairosvg** for SVG rasterization
- **ffmpeg** (subprocess) for audio + video + GIF
- **pdfplumber / pdf2docx / pdf2image / openpyxl / python-pptx** for PDF → office
- **libreoffice --headless** for office ↔ office and office → PDF
- **reportlab** for plain text → PDF
- **calibre's ebook-convert** + **pandoc** for e-books
- **stdlib zipfile/tarfile + py7zr** for archives
- **trimesh** for 3D meshes

Adding a new format pair: write a handler with signature
``(in_path, out_path, src, dst)`` and register it in ``HANDLERS``.
No new endpoint needed — see ``freeconvert.convert``.
"""
import os
import csv
import json
import shutil
import logging
import subprocess
import tempfile

log = logging.getLogger(__name__)


# --- Format groups -----------------------------------------------------------
IMAGE_FMTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff", "tif", "ico", "heic"}
AUDIO_FMTS = {"mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "wma"}
VIDEO_FMTS = {"mp4", "webm", "mov", "avi", "mkv", "flv", "wmv", "m4v", "mpeg", "mpg"}


def _norm(fmt):
    """Lowercase + strip dots; map common aliases ('jpeg'→'jpg', 'powerpoint'→'pptx',
    'word'→'docx', 'excel'→'xlsx', 'text'→'txt')."""
    s = (fmt or "").lower().strip().lstrip(".")
    aliases = {
        "jpeg": "jpg",
        "tif": "tiff",
        "powerpoint": "pptx",
        "ppt": "pptx",
        "word": "docx",
        "doc": "docx",
        "excel": "xlsx",
        "xls": "xlsx",
        "text": "txt",
        "video": "mp4",
        "audio": "mp3",
        "markdown": "md",
        "openoffice-text": "odt",
        "opendocument-text": "odt",
        "opendocument-spreadsheet": "ods",
        "opendocument-presentation": "odp",
    }
    return aliases.get(s, s)


# --- Relative compute cost (arbitrary units; useful for backpressure/queuing) ---
COST_BY_CATEGORY = {
    "image": 200,
    "image_pdf": 300,
    "audio": 200,
    "video": 500,
    "video_audio": 200,
    "video_gif": 300,
    "subtitle": 100,
    "data": 100,
    "pdf_to_word": 1000,
    "pdf_to_excel": 1000,
    "pdf_to_pptx": 1500,
    "pdf_to_text": 200,
    "pdf_to_image": 500,
    "office_to_pdf": 1500,
    "document": 500,
    "ebook": 1000,
    "archive": 500,
    "mesh": 500,
}


# --- Image conversions (Pillow + plugins) ----------------------------------
def _img_convert(in_path, out_path, src, dst):
    from PIL import Image
    if src == "heic":
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            raise RuntimeError("pillow-heif not installed (pip install freeai-convert[heic])")
    img = Image.open(in_path)
    if dst == "jpg" and img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = bg
    save_fmt = "JPEG" if dst == "jpg" else dst.upper()
    img.save(out_path, save_fmt, quality=95)


def _svg_to_png(in_path, out_path, src, dst):
    import cairosvg
    cairosvg.svg2png(url=in_path, write_to=out_path, output_width=1024)


# --- Image → PDF -------------------------------------------------------------
def _img_to_pdf(in_path, out_path, src, dst):
    from PIL import Image
    if src == "heic":
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            raise RuntimeError("pillow-heif not installed (pip install freeai-convert[heic])")
    img = Image.open(in_path)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.save(out_path, "PDF", resolution=100.0)


# --- Audio (ffmpeg) ---------------------------------------------------------
def _audio_convert(in_path, out_path, src, dst):
    codec_args = {
        "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
        "wav": ["-c:a", "pcm_s16le"],
        "flac": ["-c:a", "flac"],
        "m4a": ["-c:a", "aac", "-b:a", "192k"],
        "aac": ["-c:a", "aac", "-b:a", "192k"],
        "ogg": ["-c:a", "libvorbis", "-q:a", "5"],
        "opus": ["-c:a", "libopus", "-b:a", "128k"],
        "wma": ["-c:a", "wmav2", "-b:a", "192k"],
    }
    args = codec_args.get(dst, ["-c:a", "copy"])
    cmd = ["ffmpeg", "-y", "-i", in_path] + args + [out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg audio convert failed: {(r.stderr or '')[-300:]}")


# --- Video (ffmpeg) ---------------------------------------------------------
def _video_convert(in_path, out_path, src, dst):
    args = {
        "mp4":  ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac", "-movflags", "+faststart"],
        "webm": ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-c:a", "libopus"],
        "mov":  ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac"],
        "avi":  ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "mp3"],
        "mkv":  ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac"],
        "flv":  ["-c:v", "libx264", "-preset", "fast", "-c:a", "aac"],
    }.get(dst, ["-c:v", "libx264", "-c:a", "aac"])
    cmd = ["ffmpeg", "-y", "-i", in_path] + args + [out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg video convert failed: {(r.stderr or '')[-300:]}")


def _video_to_audio(in_path, out_path, src, dst):
    codec = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
             "wav": ["-c:a", "pcm_s16le"],
             "flac": ["-c:a", "flac"],
             "ogg": ["-c:a", "libvorbis", "-q:a", "5"],
             "aac": ["-c:a", "aac", "-b:a", "192k"]}.get(dst, ["-c:a", "copy"])
    cmd = ["ffmpeg", "-y", "-i", in_path, "-vn"] + codec + [out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg video→audio failed: {(r.stderr or '')[-300:]}")


def _video_to_gif(in_path, out_path, src, dst):
    pal = out_path + ".palette.png"
    try:
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-vf",
             "fps=10,scale=480:-1:flags=lanczos,palettegen", pal],
            capture_output=True, text=True, timeout=120,
        )
        if r1.returncode != 0:
            raise RuntimeError(f"palettegen failed: {(r1.stderr or '')[-200:]}")
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-i", pal, "-filter_complex",
             "fps=10,scale=480:-1:flags=lanczos[x];[x][1:v]paletteuse",
             out_path],
            capture_output=True, text=True, timeout=120,
        )
        if r2.returncode != 0:
            raise RuntimeError(f"paletteuse failed: {(r2.stderr or '')[-200:]}")
    finally:
        try: os.unlink(pal)
        except OSError: pass


# --- Subtitle conversions ---------------------------------------------------
def _srt_to_vtt(in_path, out_path, src, dst):
    """SRT → VTT. Only swaps comma-decimal → period-decimal *inside timestamp
    lines* — a blanket ``body.replace(',', '.')`` would corrupt dialogue."""
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        body = f.read()
    fixed_lines = []
    for ln in body.splitlines():
        if "-->" in ln:
            ln = ln.replace(",", ".")
        fixed_lines.append(ln)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n" + "\n".join(fixed_lines))


def _vtt_to_srt(in_path, out_path, src, dst):
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        body = f.read()
    lines = body.splitlines()
    out_lines, idx, i = [], 1, 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln in ("WEBVTT",) or ln.startswith("WEBVTT"):
            i += 1
            continue
        if not ln:
            i += 1
            continue
        if "-->" in ln:
            ts = ln.replace(".", ",")
            out_lines.append(str(idx))
            out_lines.append(ts)
            i += 1
            while i < len(lines) and lines[i].strip():
                out_lines.append(lines[i])
                i += 1
            out_lines.append("")
            idx += 1
        else:
            i += 1
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))


def _srt_to_txt(in_path, out_path, src, dst):
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        body = f.read()
    out, in_block = [], False
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            in_block = False
            continue
        if s.isdigit() and len(s) <= 6:
            in_block = False
            continue
        if "-->" in s:
            in_block = True
            continue
        out.append(ln)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


# --- CSV ↔ JSON --------------------------------------------------------------
def _csv_to_json(in_path, out_path, src, dst):
    rows = []
    with open(in_path, "r", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


def _json_to_csv(in_path, out_path, src, dst):
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise RuntimeError("JSON must be an array of objects")
    keys = list({k for row in data if isinstance(row, dict) for k in row.keys()})
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in data:
            if isinstance(row, dict):
                w.writerow({k: row.get(k, "") for k in keys})


# --- Data format expansion (json/xml/yaml/tsv/html/sql/markdown) ------------
# pandas + yaml + xml stdlib do the heavy lifting; one _read_tabular reader
# loads any tabular source as a DataFrame, every writer is a thin wrapper.

def _read_tabular(in_path, src):
    """Load any tabular source (csv/tsv/json/xml/yaml/xlsx/ods/html) as a
    pandas DataFrame."""
    import pandas as pd
    if src == "csv":
        return pd.read_csv(in_path)
    if src == "tsv":
        return pd.read_csv(in_path, sep="\t")
    if src == "json":
        try:
            return pd.read_json(in_path)
        except ValueError:
            return pd.read_json(in_path, lines=True)
    if src == "yaml":
        import yaml as _yaml
        with open(in_path, "r", encoding="utf-8", errors="replace") as f:
            data = _yaml.safe_load(f)
        if isinstance(data, dict):
            data = [data]
        return pd.DataFrame(data)
    if src == "xml":
        return pd.read_xml(in_path)
    if src in ("xlsx", "xls"):
        return pd.read_excel(in_path)
    if src == "ods":
        return pd.read_excel(in_path, engine="odf")
    if src == "html":
        tables = pd.read_html(in_path)
        if not tables:
            raise RuntimeError("No tables found in HTML")
        return tables[0]
    raise RuntimeError(f"unsupported data source: {src}")


def _csv_to_tsv(in_path, out_path, src, dst):
    _read_tabular(in_path, "csv").to_csv(out_path, sep="\t", index=False)
def _tsv_to_csv(in_path, out_path, src, dst):
    _read_tabular(in_path, "tsv").to_csv(out_path, index=False)
def _tsv_to_json(in_path, out_path, src, dst):
    _read_tabular(in_path, "tsv").to_json(out_path, orient="records", indent=2)
def _json_to_tsv(in_path, out_path, src, dst):
    _read_tabular(in_path, "json").to_csv(out_path, sep="\t", index=False)
def _json_to_xml(in_path, out_path, src, dst):
    _read_tabular(in_path, "json").to_xml(out_path, index=False)
def _xml_to_json(in_path, out_path, src, dst):
    _read_tabular(in_path, "xml").to_json(out_path, orient="records", indent=2)
def _xml_to_csv(in_path, out_path, src, dst):
    _read_tabular(in_path, "xml").to_csv(out_path, index=False)
def _csv_to_xml(in_path, out_path, src, dst):
    _read_tabular(in_path, "csv").to_xml(out_path, index=False)


def _json_to_yaml(in_path, out_path, src, dst):
    import yaml as _yaml
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    with open(out_path, "w", encoding="utf-8") as f:
        _yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _yaml_to_json(in_path, out_path, src, dst):
    import yaml as _yaml
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        data = _yaml.safe_load(f)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _yaml_to_csv(in_path, out_path, src, dst):
    _read_tabular(in_path, "yaml").to_csv(out_path, index=False)


def _csv_to_yaml(in_path, out_path, src, dst):
    import yaml as _yaml
    df = _read_tabular(in_path, "csv")
    with open(out_path, "w", encoding="utf-8") as f:
        _yaml.safe_dump(df.to_dict(orient="records"), f, allow_unicode=True, sort_keys=False)


def _csv_to_html(in_path, out_path, src, dst):
    _read_tabular(in_path, "csv").to_html(out_path, index=False)


def _csv_to_markdown(in_path, out_path, src, dst):
    md = _read_tabular(in_path, "csv").to_markdown(index=False) or ""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)


def _json_to_markdown(in_path, out_path, src, dst):
    md = _read_tabular(in_path, "json").to_markdown(index=False) or ""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)


def _csv_to_sql(in_path, out_path, src, dst):
    """CREATE TABLE + INSERT statements. TEXT-typed for portability across
    SQLite / Postgres / MySQL."""
    df = _read_tabular(in_path, "csv")
    table = os.path.splitext(os.path.basename(in_path))[0].replace("-", "_").replace(" ", "_") or "data"
    cols = ", ".join(f'"{c}" TEXT' for c in df.columns)
    lines = [f'CREATE TABLE "{table}" ({cols});']
    for _, row in df.iterrows():
        vals = ", ".join("'" + str(v).replace("'", "''") + "'" for v in row.values)
        lines.append(f'INSERT INTO "{table}" VALUES ({vals});')
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# --- Subtitle format expansion (ass / sbv via SRT-cue pivot) ----------------

def _parse_srt_full(in_path):
    import re as _re
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        body = f.read()
    cues = []
    for block in _re.split(r"\n\s*\n", body.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        ts_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ts_idx is None:
            continue
        text = "\n".join(lines[ts_idx + 1:])
        m = _re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[ts_idx])
        if not m: continue
        s = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000
        e = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000
        cues.append((s, e, text))
    return cues


def _parse_vtt_full(in_path):
    import re as _re
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        body = f.read()
    body = _re.sub(r"^WEBVTT.*?\n\n", "", body, flags=_re.S)
    cues = []
    for block in _re.split(r"\n\s*\n", body.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        ts_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ts_idx is None: continue
        text = "\n".join(lines[ts_idx + 1:])
        m = _re.search(r"(\d+):(\d+)[:.](\d+)\.(\d+)\s*-->\s*(\d+):(\d+)[:.](\d+)\.(\d+)", lines[ts_idx])
        if not m: continue
        s = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000
        e = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000
        cues.append((s, e, text))
    return cues


def _parse_ass(in_path):
    import re as _re
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        body = f.read()
    cues = []
    in_events = False
    fmt_keys = []
    for ln in body.splitlines():
        s = ln.strip()
        if s.lower().startswith("[events]"):
            in_events = True; continue
        if in_events and s.lower().startswith("format:"):
            fmt_keys = [k.strip().lower() for k in s.split(":", 1)[1].split(",")]; continue
        if in_events and s.lower().startswith("dialogue:"):
            parts = s.split(":", 1)[1].split(",", len(fmt_keys) - 1)
            if len(parts) < len(fmt_keys): continue
            d = dict(zip(fmt_keys, parts))
            try:
                start = _ass_ts_to_sec(d.get("start", "0:00:00.00"))
                end = _ass_ts_to_sec(d.get("end", "0:00:00.00"))
            except Exception:
                continue
            text = d.get("text", "").replace(r"\N", "\n").replace(r"\n", "\n")
            text = _re.sub(r"\{[^}]*\}", "", text)
            cues.append((start, end, text))
    return cues


def _ass_ts_to_sec(ts):
    h, m, rest = ts.split(":", 2)
    s, cs = (rest.split(".", 1) + ["0"])[:2]
    return int(h)*3600 + int(m)*60 + int(s) + int(cs)/100


def _parse_sbv(in_path):
    import re as _re
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        body = f.read()
    cues = []
    for block in _re.split(r"\n\s*\n", body.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines or "," not in lines[0]: continue
        m = _re.match(r"(\d+):(\d+):(\d+)\.(\d+),(\d+):(\d+):(\d+)\.(\d+)", lines[0])
        if not m: continue
        s = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000
        e = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000
        cues.append((s, e, "\n".join(lines[1:])))
    return cues


def _fmt_srt_ts(seconds):
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_vtt_ts(seconds):
    return _fmt_srt_ts(seconds).replace(",", ".")


def _fmt_ass_ts(seconds):
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _write_srt(cues, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        for i, (s, e, t) in enumerate(cues, 1):
            f.write(f"{i}\n{_fmt_srt_ts(s)} --> {_fmt_srt_ts(e)}\n{t}\n\n")


def _write_vtt(cues, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for s, e, t in cues:
            f.write(f"{_fmt_vtt_ts(s)} --> {_fmt_vtt_ts(e)}\n{t}\n\n")


def _write_ass(cues, out_path):
    head = (
        "[Script Info]\nScriptType: v4.00+\nCollisions: Normal\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,1,2,10,10,10,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(head)
        for s, e, t in cues:
            t_ass = t.replace("\n", r"\N")
            f.write(f"Dialogue: 0,{_fmt_ass_ts(s)},{_fmt_ass_ts(e)},Default,,0,0,0,,{t_ass}\n")


def _write_sbv(cues, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        for s, e, t in cues:
            f.write(f"{_fmt_vtt_ts(s)},{_fmt_vtt_ts(e)}\n{t}\n\n")


def _write_txt_cues(cues, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        for _, _, t in cues:
            f.write(t.strip() + "\n")


def _ass_to_srt(in_path, out_path, src, dst):  _write_srt(_parse_ass(in_path), out_path)
def _ass_to_vtt(in_path, out_path, src, dst):  _write_vtt(_parse_ass(in_path), out_path)
def _ass_to_txt(in_path, out_path, src, dst):  _write_txt_cues(_parse_ass(in_path), out_path)
def _srt_to_ass(in_path, out_path, src, dst):  _write_ass(_parse_srt_full(in_path), out_path)
def _vtt_to_ass(in_path, out_path, src, dst):  _write_ass(_parse_vtt_full(in_path), out_path)
def _vtt_to_txt(in_path, out_path, src, dst):  _write_txt_cues(_parse_vtt_full(in_path), out_path)
def _sbv_to_srt(in_path, out_path, src, dst):  _write_srt(_parse_sbv(in_path), out_path)
def _sbv_to_vtt(in_path, out_path, src, dst):  _write_vtt(_parse_sbv(in_path), out_path)
def _srt_to_sbv(in_path, out_path, src, dst):  _write_sbv(_parse_srt_full(in_path), out_path)
def _vtt_to_sbv(in_path, out_path, src, dst):  _write_sbv(_parse_vtt_full(in_path), out_path)


# --- PDF → {word, excel, pptx, text, image} ---------------------------------
def _pdf_to_text(in_path, out_path, src, dst):
    import pdfplumber
    parts = []
    with pdfplumber.open(in_path) as pdf:
        for p in pdf.pages:
            t = p.extract_text() or ""
            if t:
                parts.append(t)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(parts))


def _pdf_to_word(in_path, out_path, src, dst):
    try:
        from pdf2docx import Converter
        cv = Converter(in_path)
        try:
            cv.convert(out_path, start=0, end=None)
        finally:
            cv.close()
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return
    except Exception:
        pass
    import pdfplumber
    from docx import Document
    doc = Document()
    with pdfplumber.open(in_path) as pdf:
        for pg in pdf.pages:
            t = pg.extract_text() or ""
            if t:
                for para in t.split("\n\n"):
                    doc.add_paragraph(para)
            doc.add_page_break()
    doc.save(out_path)


def _pdf_to_excel(in_path, out_path, src, dst):
    import pdfplumber
    from openpyxl import Workbook
    wb = Workbook()
    overview = wb.active
    overview.title = "Overview"
    written = 0
    with pdfplumber.open(in_path) as pdf:
        for pi, page in enumerate(pdf.pages):
            try:
                tbls = page.extract_tables() or []
            except Exception:
                tbls = []
            for ti, table in enumerate(tbls, start=1):
                if not table:
                    continue
                ws = wb.create_sheet(title=f"P{pi+1}_T{ti}"[:31])
                for row in table:
                    ws.append([("" if c is None else str(c)) for c in row])
                written += 1
    overview.append(["Tables Extracted", written])
    if written == 0:
        overview.append(["Note", "No tables detected — try PDF→Word for plain text."])
    wb.save(out_path)


def _pdf_to_pptx(in_path, out_path, src, dst):
    from pptx import Presentation
    from pptx.util import Inches
    from pdf2image import convert_from_path
    images = convert_from_path(in_path, dpi=120)
    prs = Presentation()
    prs.slide_width = Inches(13.33)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]
    tmp_dir = tempfile.mkdtemp()
    try:
        for i, img in enumerate(images):
            img_path = os.path.join(tmp_dir, f"slide_{i}.png")
            img.save(img_path, "PNG")
            slide = prs.slides.add_slide(blank)
            slide.shapes.add_picture(img_path, Inches(0), Inches(0),
                                     width=prs.slide_width, height=prs.slide_height)
        prs.save(out_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _pdf_to_image(in_path, out_path, src, dst):
    from pdf2image import convert_from_path
    images = convert_from_path(in_path, dpi=150, first_page=1, last_page=1)
    if not images:
        raise RuntimeError("PDF has no renderable pages")
    fmt = "JPEG" if dst == "jpg" else "PNG"
    images[0].save(out_path, fmt)


# --- LibreOffice headless dispatcher ----------------------------------------
def _libreoffice_convert(in_path, out_path, src, dst):
    """Run ``libreoffice --headless --convert-to <dst>``. Each call gets an
    isolated profile dir so concurrent conversions don't trip the
    "another instance is running" lock."""
    workdir = tempfile.mkdtemp(prefix="lo_")
    profile = tempfile.mkdtemp(prefix="lo_prof_")
    try:
        cmd = [
            "libreoffice",
            "--headless",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", dst,
            "--outdir", workdir,
            in_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError(f"libreoffice convert failed: {(r.stderr or r.stdout or '')[-300:]}")
        base = os.path.splitext(os.path.basename(in_path))[0]
        produced = os.path.join(workdir, f"{base}.{dst}")
        if not os.path.exists(produced):
            for fn in os.listdir(workdir):
                if fn.lower().endswith(f".{dst}"):
                    produced = os.path.join(workdir, fn)
                    break
        if not os.path.exists(produced):
            raise RuntimeError(f"libreoffice produced no .{dst} output")
        shutil.move(produced, out_path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


# --- Text format helpers ----------------------------------------------------
def _txt_to_pdf(in_path, out_path, src, dst):
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Preformatted
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    pdf = SimpleDocTemplate(out_path, pagesize=letter,
                            leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    pdf.build([Preformatted(text, styles["Code"])])


def _txt_to_docx(in_path, out_path, src, dst):
    from docx import Document
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    doc = Document()
    for line in text.splitlines() or [""]:
        doc.add_paragraph(line)
    doc.save(out_path)


def _md_to_html(in_path, out_path, src, dst):
    """Markdown → HTML via stdlib + minimal regex. Covers headings, bold,
    italic, code, links, paragraphs, simple lists. For full CommonMark
    fidelity, install the ``markdown`` package and replace this handler."""
    import re, html
    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        body = f.read()
    out_lines = []
    in_list = False
    for raw in body.splitlines():
        line = raw.rstrip()
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if in_list: out_lines.append("</ul>"); in_list = False
            level = len(m.group(1))
            out_lines.append(f"<h{level}>{html.escape(m.group(2))}</h{level}>")
            continue
        if line.startswith("- ") or line.startswith("* "):
            if not in_list: out_lines.append("<ul>"); in_list = True
            out_lines.append(f"  <li>{html.escape(line[2:])}</li>")
            continue
        if in_list and not line:
            out_lines.append("</ul>"); in_list = False
        if not line:
            continue
        s = html.escape(line)
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'\*(.+?)\*', r'<em>\1</em>', s)
        s = re.sub(r'`(.+?)`', r'<code>\1</code>', s)
        s = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', s)
        out_lines.append(f"<p>{s}</p>")
    if in_list: out_lines.append("</ul>")
    html_doc = (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
        "<title>Converted</title>"
        "<style>body{max-width:720px;margin:2em auto;font-family:system-ui,sans-serif;line-height:1.5;padding:0 1em}"
        "code{background:#f4f4f4;padding:1px 4px;border-radius:3px}"
        "pre{background:#f4f4f4;padding:1em;border-radius:6px;overflow-x:auto}</style></head><body>\n"
        + "\n".join(out_lines) + "\n</body></html>"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)


def _md_to_pdf(in_path, out_path, src, dst):
    tmp_html = tempfile.mktemp(suffix=".html")
    try:
        _md_to_html(in_path, tmp_html, "md", "html")
        _libreoffice_convert(tmp_html, out_path, "html", "pdf")
    finally:
        try: os.unlink(tmp_html)
        except OSError: pass


# --- E-books (calibre + pandoc) --------------------------------------------
def _ebook_convert(in_path, out_path, src, dst):
    """Calibre's ebook-convert handles every e-book format pair, including
    mobi/azw3 which pandoc can't read. Slower than pandoc (~10-30s) but
    universal."""
    cmd = ["ebook-convert", in_path, out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"ebook-convert failed: {(r.stderr or r.stdout or '')[-300:]}")


def _pandoc_convert(in_path, out_path, src, dst):
    """Pandoc — fast (~1-3s) for docx/html/md/epub/rst/textile pairs."""
    src_filter = {"md": "markdown", "html": "html"}.get(src, src)
    dst_filter = {"md": "markdown", "html": "html"}.get(dst, dst)
    cmd = ["pandoc", "-f", src_filter, "-t", dst_filter, "-o", out_path, in_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        try:
            _ebook_convert(in_path, out_path, src, dst)
        except Exception:
            raise RuntimeError(f"pandoc failed: {(r.stderr or '')[-300:]}")


# --- Archives (stdlib zipfile/tarfile + py7zr) ------------------------------
def _archive_convert(in_path, out_path, src, dst):
    """Re-package archive contents from src format to dst format. Supports
    zip ↔ tar / tar.gz / tgz / tar.bz2 / bz2 / xz / 7z. Files are extracted
    to a tempdir then re-packed via the dst format's writer."""
    import zipfile, tarfile
    workdir = tempfile.mkdtemp(prefix="arch_")
    try:
        if src == "zip":
            with zipfile.ZipFile(in_path) as z:
                z.extractall(workdir)
        elif src in ("tar", "tar.gz", "tgz", "tar.bz2", "tbz", "tar.xz", "txz", "gz", "bz2", "xz"):
            # tarfile.open(..., "r:*") auto-detects compression for every variant
            with tarfile.open(in_path, "r:*") as t:
                t.extractall(workdir)
        elif src == "7z":
            import py7zr
            with py7zr.SevenZipFile(in_path) as z:
                z.extractall(workdir)
        else:
            raise RuntimeError(f"Unsupported archive source: {src}")

        if dst == "zip":
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
                for root, _, files in os.walk(workdir):
                    for fn in files:
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, workdir)
                        z.write(full, rel)
        elif dst in ("tar", "tar.gz", "tgz", "gz", "bz2", "xz"):
            mode_map = {"tar": "w", "tar.gz": "w:gz", "tgz": "w:gz",
                        "gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz"}
            with tarfile.open(out_path, mode_map[dst]) as t:
                t.add(workdir, arcname=".")
        elif dst == "7z":
            import py7zr
            with py7zr.SevenZipFile(out_path, "w") as z:
                z.writeall(workdir, arcname="")
        else:
            raise RuntimeError(f"Unsupported archive target: {dst}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- 3D meshes (trimesh) ----------------------------------------------------
def _mesh_convert(in_path, out_path, src, dst):
    import trimesh
    mesh = trimesh.load(in_path, file_type=src, force="mesh")
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.dump(concatenate=True)
    mesh.export(out_path, file_type=dst)


# --- Spreadsheet ↔ data ----------------------------------------------------
def _xlsx_to_csv(in_path, out_path, src, dst):
    from openpyxl import load_workbook
    wb = load_workbook(in_path, data_only=True)
    ws = wb.worksheets[0]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for row in ws.iter_rows(values_only=True):
            w.writerow(["" if c is None else c for c in row])


def _csv_to_xlsx(in_path, out_path, src, dst):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    with open(in_path, "r", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            ws.append(row)
    wb.save(out_path)


def _xlsx_to_json(in_path, out_path, src, dst):
    """First-sheet XLSX → JSON array of objects (header row = keys)."""
    from openpyxl import load_workbook
    wb = load_workbook(in_path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        with open(out_path, "w") as f: f.write("[]")
        return
    headers = [str(h) if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
    out = []
    for row in rows[1:]:
        out.append({headers[i]: ("" if v is None else v) for i, v in enumerate(row) if i < len(headers)})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)


# --- Dispatch table ---------------------------------------------------------
# (src, dst) → (handler, category). Handler signature: (in_path, out_path, src, dst).
HANDLERS = {}

# Image ↔ image
for _src in IMAGE_FMTS:
    for _dst in IMAGE_FMTS:
        if _src != _dst:
            HANDLERS[(_src, _dst)] = (_img_convert, "image")

# Image → PDF
for _src in IMAGE_FMTS:
    HANDLERS[(_src, "pdf")] = (_img_to_pdf, "image_pdf")

# SVG → PNG / JPG
HANDLERS[("svg", "png")] = (_svg_to_png, "image")
HANDLERS[("svg", "jpg")] = (_svg_to_png, "image")

# Audio ↔ audio
for _src in AUDIO_FMTS:
    for _dst in AUDIO_FMTS:
        if _src != _dst:
            HANDLERS[(_src, _dst)] = (_audio_convert, "audio")

# Video ↔ video
for _src in VIDEO_FMTS:
    for _dst in VIDEO_FMTS:
        if _src != _dst:
            HANDLERS[(_src, _dst)] = (_video_convert, "video")

# Video → audio
for _src in VIDEO_FMTS:
    for _dst in AUDIO_FMTS:
        HANDLERS[(_src, _dst)] = (_video_to_audio, "video_audio")

# Video → GIF
for _src in VIDEO_FMTS:
    HANDLERS[(_src, "gif")] = (_video_to_gif, "video_gif")

# Subtitles
HANDLERS[("srt", "vtt")] = (_srt_to_vtt, "subtitle")
HANDLERS[("vtt", "srt")] = (_vtt_to_srt, "subtitle")
HANDLERS[("srt", "txt")] = (_srt_to_txt, "subtitle")

# Data
HANDLERS[("csv", "json")] = (_csv_to_json, "data")
HANDLERS[("json", "csv")] = (_json_to_csv, "data")

# PDF → office
HANDLERS[("pdf", "docx")] = (_pdf_to_word, "pdf_to_word")
HANDLERS[("pdf", "xlsx")] = (_pdf_to_excel, "pdf_to_excel")
HANDLERS[("pdf", "pptx")] = (_pdf_to_pptx, "pdf_to_pptx")
HANDLERS[("pdf", "txt")] = (_pdf_to_text, "pdf_to_text")
HANDLERS[("pdf", "png")] = (_pdf_to_image, "pdf_to_image")
HANDLERS[("pdf", "jpg")] = (_pdf_to_image, "pdf_to_image")

# Office → PDF
HANDLERS[("docx", "pdf")] = (_libreoffice_convert, "office_to_pdf")
HANDLERS[("xlsx", "pdf")] = (_libreoffice_convert, "office_to_pdf")
HANDLERS[("pptx", "pdf")] = (_libreoffice_convert, "office_to_pdf")
HANDLERS[("odt", "pdf")] = (_libreoffice_convert, "office_to_pdf")
HANDLERS[("ods", "pdf")] = (_libreoffice_convert, "office_to_pdf")
HANDLERS[("rtf", "pdf")] = (_libreoffice_convert, "office_to_pdf")
HANDLERS[("html", "pdf")] = (_libreoffice_convert, "office_to_pdf")

# Office ↔ office
HANDLERS[("docx", "odt")] = (_libreoffice_convert, "document")
HANDLERS[("odt", "docx")] = (_libreoffice_convert, "document")
HANDLERS[("docx", "txt")] = (_libreoffice_convert, "document")
HANDLERS[("docx", "html")] = (_libreoffice_convert, "document")
HANDLERS[("docx", "rtf")] = (_libreoffice_convert, "document")
HANDLERS[("rtf", "docx")] = (_libreoffice_convert, "document")
HANDLERS[("html", "docx")] = (_libreoffice_convert, "document")
HANDLERS[("xlsx", "ods")] = (_libreoffice_convert, "document")
HANDLERS[("ods", "xlsx")] = (_libreoffice_convert, "document")
HANDLERS[("xlsx", "html")] = (_libreoffice_convert, "document")
HANDLERS[("pptx", "odp")] = (_libreoffice_convert, "document")
HANDLERS[("odp", "pptx")] = (_libreoffice_convert, "document")
HANDLERS[("pptx", "png")] = (_libreoffice_convert, "document")
HANDLERS[("pptx", "jpg")] = (_libreoffice_convert, "document")

# Plain text & markdown
HANDLERS[("txt", "pdf")] = (_txt_to_pdf, "document")
HANDLERS[("txt", "docx")] = (_txt_to_docx, "document")
HANDLERS[("txt", "html")] = (_libreoffice_convert, "document")
HANDLERS[("md", "html")] = (_md_to_html, "document")
HANDLERS[("md", "pdf")] = (_md_to_pdf, "document")
HANDLERS[("md", "docx")] = (_libreoffice_convert, "document")

# Spreadsheet ↔ data
HANDLERS[("xlsx", "csv")] = (_xlsx_to_csv, "data")
HANDLERS[("csv", "xlsx")] = (_csv_to_xlsx, "data")
HANDLERS[("ods", "csv")] = (_xlsx_to_csv, "data")
HANDLERS[("csv", "ods")] = (_libreoffice_convert, "data")
HANDLERS[("xlsx", "json")] = (_xlsx_to_json, "data")

# E-books (every pair via calibre; pandoc fast-path for the docx/html/md subset)
_EBOOK_FMTS = {"epub", "mobi", "azw3", "fb2", "lit"}
for _src in _EBOOK_FMTS:
    for _dst in _EBOOK_FMTS:
        if _src != _dst:
            HANDLERS[(_src, _dst)] = (_ebook_convert, "ebook")
for _eb in _EBOOK_FMTS:
    for _doc in ("pdf", "docx", "html", "md", "txt", "rtf"):
        handler = _pandoc_convert if _eb == "epub" else _ebook_convert
        HANDLERS[(_eb, _doc)] = (handler, "ebook")
        if _doc in ("docx", "html", "md", "rtf"):
            HANDLERS[(_doc, _eb)] = (_ebook_convert if _eb != "epub" else _pandoc_convert, "ebook")
HANDLERS[("pdf", "epub")] = (_ebook_convert, "ebook")
HANDLERS[("pdf", "mobi")] = (_ebook_convert, "ebook")
HANDLERS[("pdf", "azw3")] = (_ebook_convert, "ebook")
HANDLERS[("pdf", "fb2")] = (_ebook_convert, "ebook")

# Data (json/xml/yaml/tsv/html/markdown/sql) — pandas + yaml + xml stdlib
HANDLERS[("csv", "tsv")] = (_csv_to_tsv, "data")
HANDLERS[("tsv", "csv")] = (_tsv_to_csv, "data")
HANDLERS[("tsv", "json")] = (_tsv_to_json, "data")
HANDLERS[("json", "tsv")] = (_json_to_tsv, "data")
HANDLERS[("json", "xml")] = (_json_to_xml, "data")
HANDLERS[("xml", "json")] = (_xml_to_json, "data")
HANDLERS[("xml", "csv")] = (_xml_to_csv, "data")
HANDLERS[("csv", "xml")] = (_csv_to_xml, "data")
HANDLERS[("json", "yaml")] = (_json_to_yaml, "data")
HANDLERS[("yaml", "json")] = (_yaml_to_json, "data")
HANDLERS[("json", "yml")] = (_json_to_yaml, "data")
HANDLERS[("yml", "json")] = (_yaml_to_json, "data")
HANDLERS[("yaml", "csv")] = (_yaml_to_csv, "data")
HANDLERS[("csv", "yaml")] = (_csv_to_yaml, "data")
HANDLERS[("csv", "html")] = (_csv_to_html, "data")
HANDLERS[("csv", "md")] = (_csv_to_markdown, "data")
HANDLERS[("json", "md")] = (_json_to_markdown, "data")
HANDLERS[("csv", "sql")] = (_csv_to_sql, "data")

# Subtitle expansion (ass + sbv via SRT-cue pivot)
HANDLERS[("ass", "srt")] = (_ass_to_srt, "subtitle")
HANDLERS[("ass", "vtt")] = (_ass_to_vtt, "subtitle")
HANDLERS[("ass", "txt")] = (_ass_to_txt, "subtitle")
HANDLERS[("srt", "ass")] = (_srt_to_ass, "subtitle")
HANDLERS[("vtt", "ass")] = (_vtt_to_ass, "subtitle")
HANDLERS[("vtt", "txt")] = (_vtt_to_txt, "subtitle")
HANDLERS[("sbv", "srt")] = (_sbv_to_srt, "subtitle")
HANDLERS[("sbv", "vtt")] = (_sbv_to_vtt, "subtitle")
HANDLERS[("srt", "sbv")] = (_srt_to_sbv, "subtitle")
HANDLERS[("vtt", "sbv")] = (_vtt_to_sbv, "subtitle")

# Archives — expanded to cover tar.gz/.bz2/.xz + bare gz/bz2/xz aliases
# (was just zip/tar/tar.gz/tgz/7z). _archive_convert handles all of them.
_ARCHIVE_FMTS = {"zip", "tar", "tar.gz", "tgz", "gz", "bz2", "xz", "7z"}
for _src in _ARCHIVE_FMTS:
    for _dst in _ARCHIVE_FMTS:
        if _src != _dst:
            HANDLERS[(_src, _dst)] = (_archive_convert, "archive")

# 3D meshes — expanded to include 3mf, dae, off (trimesh handles them
# natively). FBX + USDZ intentionally skipped (closed-source SDK / heavy
# dep respectively).
_MESH_FMTS = {"obj", "stl", "ply", "glb", "gltf", "3mf", "dae", "off"}
for _src in _MESH_FMTS:
    for _dst in _MESH_FMTS:
        if _src != _dst:
            HANDLERS[(_src, _dst)] = (_mesh_convert, "mesh")


def lookup(src, dst):
    """Resolve a ``(src, dst)`` format pair to ``(handler, category, cost, src, dst)``.

    Aliases are normalized (``jpeg`` → ``jpg``, ``word`` → ``docx``, etc.).
    Returns ``None`` if no handler is registered for the pair.

    >>> handler, category, cost, src, dst = lookup("jpeg", "png")
    >>> category
    'image'
    """
    src, dst = _norm(src), _norm(dst)
    entry = HANDLERS.get((src, dst))
    if not entry:
        return None
    handler, category = entry
    cost = COST_BY_CATEGORY.get(category, 200)
    return handler, category, cost, src, dst


def supported_pairs():
    """All registered ``(src, dst)`` tuples, sorted."""
    return sorted(HANDLERS.keys())
