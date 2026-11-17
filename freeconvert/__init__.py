"""freeconvert — universal file format converter.

>>> from freeconvert import convert
>>> convert("input.png", "output.jpg")        # infer formats from extensions
>>> convert("doc.pdf", "doc.docx")            # PDF → Word via pdf2docx
>>> convert("song.flac", "song.mp3")          # ffmpeg under the hood

For a full list of supported pairs:

>>> from freeconvert import supported_pairs
>>> ("pdf", "docx") in supported_pairs()
True
"""
from __future__ import annotations

import os
from typing import Optional

from .handlers import (
    COST_BY_CATEGORY,
    HANDLERS,
    _norm,
    lookup,
    supported_pairs,
)

__version__ = "0.2.1"

__all__ = [
    "convert",
    "lookup",
    "supported_pairs",
    "HANDLERS",
    "COST_BY_CATEGORY",
    "UnsupportedConversionError",
    "__version__",
]


class UnsupportedConversionError(ValueError):
    """Raised when no handler is registered for the requested ``(src, dst)`` pair."""


def _ext(path: str) -> str:
    """Filename → extension, stripped + lowercased. Handles double extensions
    for archives (``foo.tar.gz`` → ``tar.gz``)."""
    name = os.path.basename(path).lower()
    for compound in ("tar.gz", "tar.bz2"):
        if name.endswith("." + compound):
            return compound
    return os.path.splitext(name)[1].lstrip(".")


def convert(
    in_path: str,
    out_path: str,
    *,
    src: Optional[str] = None,
    dst: Optional[str] = None,
) -> dict:
    """Convert ``in_path`` to ``out_path``.

    Source + target formats are inferred from filename extensions unless
    ``src`` / ``dst`` are passed explicitly.

    Returns a dict with ``{src, dst, category, cost}`` for the conversion
    that ran (useful for backpressure / metrics).

    Raises ``UnsupportedConversionError`` if no handler covers the pair,
    or ``FileNotFoundError`` if ``in_path`` doesn't exist. Handler-level
    failures (ffmpeg crash, libreoffice missing) raise ``RuntimeError``.
    """
    if not os.path.exists(in_path):
        raise FileNotFoundError(in_path)
    src = src or _ext(in_path)
    dst = dst or _ext(out_path)
    if not src or not dst:
        raise UnsupportedConversionError(
            f"Could not infer formats from paths ({in_path!r} → {out_path!r}). "
            f"Pass src= and dst= explicitly."
        )
    resolved = lookup(src, dst)
    if resolved is None:
        raise UnsupportedConversionError(
            f"No handler for {_norm(src)!r} → {_norm(dst)!r}"
        )
    handler, category, cost, src_n, dst_n = resolved
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    handler(in_path, out_path, src_n, dst_n)
    return {"src": src_n, "dst": dst_n, "category": category, "cost": cost}
