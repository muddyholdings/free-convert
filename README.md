# freeai-convert

**Universal file format converter.** One dispatch table, ~80 format pairs across image / audio / video / pdf / office / ebook / csv / srt / archive / 3d.

```bash
pip install freeai-convert
freeconvert resume.pdf resume.docx
freeconvert song.flac song.mp3
freeconvert photo.heic photo.jpg
freeconvert deck.pptx deck.pdf
```

Same converter that backs every `/convert/<pair>/` page on [free.ai](https://free.ai/convert/), extracted into a standalone CLI + Python library.

## Why this exists

Most "universal converters" are either thin wrappers around `pandoc` (great for text, useless for images) or trillion-dependency monsters that take a `pip install` over a coffee break. `freeai-convert` is a **dispatch table**: one `(src_ext, dst_ext) → handler` map. Each handler is a 5–30 line wrapper around the right tool for the job — Pillow for images, ffmpeg for media, libreoffice for office, pdf2docx / pdfplumber for PDF, calibre for e-books, trimesh for 3D meshes, stdlib for csv/json/srt/vtt.

Adding a new format pair = one function + one line in `HANDLERS`. No abstraction, no plugin system, no class hierarchy.

## What's supported

Run `freeconvert --list` to see the full set. Highlights:

| From | To | Tool |
|------|----|----|
| jpg/png/webp/heic/bmp/tiff/ico/gif | any image format | Pillow (+ `pillow-heif` for HEIC) |
| any image | pdf | Pillow |
| svg | png/jpg | cairosvg |
| mp3/wav/flac/m4a/aac/ogg/opus/wma | any audio | ffmpeg |
| mp4/webm/mov/avi/mkv/flv | any video | ffmpeg |
| any video | any audio | ffmpeg (strip + re-encode) |
| any video | gif | ffmpeg (palette-gen, 2-pass) |
| pdf | docx / xlsx / pptx / txt / png / jpg | pdf2docx / pdfplumber / pdf2image |
| docx/xlsx/pptx/odt/ods/rtf/html | pdf | libreoffice --headless |
| docx ↔ odt / rtf / html / txt, xlsx ↔ ods, pptx ↔ odp | | libreoffice |
| md → html / pdf / docx | | stdlib regex / libreoffice |
| txt → pdf / docx | | reportlab / python-docx |
| csv ↔ json, xlsx ↔ csv / json, csv ↔ xlsx | | stdlib + openpyxl |
| srt ↔ vtt, srt → txt | | stdlib |
| epub/mobi/azw3/fb2/lit ↔ each other ↔ pdf/docx/html/md/txt/rtf | | calibre + pandoc |
| zip ↔ tar/tar.gz/7z | | stdlib + py7zr |
| obj ↔ stl ↔ ply ↔ glb ↔ gltf | | trimesh |

## Install

```bash
# Minimal install — CLI + dispatch table + stdlib-only handlers (csv/json/srt/vtt):
pip install freeai-convert

# Plus pure-Python image/PDF/office support:
pip install 'freeai-convert[image,pdf,office]'

# Everything pip-installable:
pip install 'freeai-convert[all]'
```

Several handlers shell out to system tools — install them via your OS package manager:

| Handler | System dep | Debian/Ubuntu | macOS (brew) |
|---|---|---|---|
| audio / video / gif | `ffmpeg` | `apt install ffmpeg` | `brew install ffmpeg` |
| pdf → pptx, pdf → png/jpg | `poppler-utils` | `apt install poppler-utils` | `brew install poppler` |
| office ↔ office, office → pdf | `libreoffice` | `apt install libreoffice` | `brew install --cask libreoffice` |
| ebook (mobi/azw3/fb2/lit) | `calibre` | `apt install calibre` | `brew install --cask calibre` |
| ebook (epub fast-path) | `pandoc` | `apt install pandoc` | `brew install pandoc` |

If you call a handler whose system tool is missing, you'll get a clear `RuntimeError` naming what failed.

## Python API

```python
from freeconvert import convert, lookup, supported_pairs, UnsupportedConversionError

# Easiest — formats inferred from extensions:
info = convert("/tmp/in.pdf", "/tmp/out.docx")
# {'src': 'pdf', 'dst': 'docx', 'category': 'pdf_to_word', 'cost': 1000}

# Explicit src/dst when extensions are wrong or missing:
convert("/tmp/blob", "/tmp/out.png", src="jpg", dst="png")

# Probe before running:
resolved = lookup("heic", "jpg")
if resolved is None:
    raise UnsupportedConversionError(...)
handler, category, cost, src, dst = resolved

# Or inspect the dispatch table directly:
("pdf", "docx") in supported_pairs()  # True
```

The `cost` field is an arbitrary relative-compute estimate (200 = light Pillow work, 1500 = libreoffice reflow). Useful for queue backpressure on a server; safe to ignore in scripts.

## Extending it

```python
from freeconvert.handlers import HANDLERS

def _yaml_to_json(in_path, out_path, src, dst):
    import yaml, json
    with open(in_path) as f: data = yaml.safe_load(f)
    with open(out_path, "w") as f: json.dump(data, f, indent=2)

HANDLERS[("yaml", "json")] = (_yaml_to_json, "data")
```

That's it — your new pair is now reachable via `convert()` and the CLI.

## Don't want to install anything? Use the hosted API

Same code, behind an HTTPS endpoint at `api.free.ai`. No signup required for the daily free pool (~10K tokens, enough for ~50 image conversions or ~10 PDF→Word).

```bash
# Convert PDF → DOCX:
curl -X POST https://api.free.ai/v1/convert/ \
  -F "file=@resume.pdf" \
  -F "to_format=docx" \
  -o resp.json

# resp.json:
# {
#   "job_id": "…",
#   "file_url": "https://api.free.ai/static/outputs/<id>.docx",
#   "from": "pdf", "to": "docx",
#   "category": "pdf_to_word", "tokens": 1000
# }
```

```bash
# List every supported pair (no auth required):
curl https://api.free.ai/v1/convert/list/
```

```python
# Python:
import httpx
with open("photo.heic", "rb") as f:
    r = httpx.post(
        "https://api.free.ai/v1/convert/",
        files={"file": f},
        data={"to_format": "jpg"},
        timeout=60,
    )
print(r.json()["file_url"])
```

For higher limits, sign up at [free.ai](https://free.ai) and pass `X-User-Id: <your-id>` (or use the OpenAI-compatible Bearer-token API key from `/account/?tab=api`). Token cost is the same `cost` field returned by `freeconvert.lookup()` — 200 for an image swap, 1000 for PDF→Word, 1500 for libreoffice reflow.

## Project layout

```
freeconvert/
├── __init__.py     # convert(), exports
├── handlers.py     # HANDLERS dispatch table + handler functions
├── cli.py          # click CLI
└── __main__.py     # python -m freeconvert
```

## License

MIT — see [LICENSE](LICENSE).

## Related

- [free-scene](https://github.com/freeaigit/free-scene) — multi-scene AI movie maker
- [free-code](https://github.com/freeaigit/free-code) — agentic coding CLI
- [free-sdk](https://github.com/freeaigit/free-sdk) — Python SDK for the Free.ai API
- [free.ai](https://free.ai) — 400+ AI tools, the home of this codebase
