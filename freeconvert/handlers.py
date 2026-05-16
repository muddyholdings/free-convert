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
    zip ↔ tar ↔ tar.gz ↔ 7z. Files are extracted to a tempdir then re-packed."""
    import zipfile, tarfile
    workdir = tempfile.mkdtemp(prefix="arch_")
    try:
        if src == "zip":
            with zipfile.ZipFile(in_path) as z:
                z.extractall(workdir)
        elif src in ("tar", "tar.gz", "tgz", "tar.bz2", "tbz"):
            with tarfile.open(in_path) as t:
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
        elif dst in ("tar", "tar.gz", "tgz"):
            mode = "w:gz" if dst != "tar" else "w"
            with tarfile.open(out_path, mode) as t:
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

# Archives
_ARCHIVE_FMTS = {"zip", "tar", "tar.gz", "tgz", "7z"}
for _src in _ARCHIVE_FMTS:
    for _dst in _ARCHIVE_FMTS:
        if _src != _dst:
            HANDLERS[(_src, _dst)] = (_archive_convert, "archive")

# 3D meshes
_MESH_FMTS = {"obj", "stl", "ply", "glb", "gltf"}
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
