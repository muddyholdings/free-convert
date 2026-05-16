"""Click-based CLI: ``freeconvert input.png output.jpg``."""
from __future__ import annotations

import sys
from pathlib import Path

import click

from . import (
    UnsupportedConversionError,
    __version__,
    convert,
    lookup,
    supported_pairs,
)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("input_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=False)
@click.argument("output_path", type=click.Path(dir_okay=False, path_type=Path), required=False)
@click.option("--from", "src", default=None, help="Source format (default: infer from input extension).")
@click.option("--to", "dst", default=None, help="Target format (default: infer from output extension).")
@click.option("--list", "list_all", is_flag=True, help="List every supported (src → dst) pair and exit.")
@click.option("--list-from", "list_from", default=None, metavar="FMT",
              help="List supported targets reachable from FMT (e.g. ``--list-from pdf``).")
@click.option("--list-to", "list_to", default=None, metavar="FMT",
              help="List supported sources that can produce FMT (e.g. ``--list-to mp3``).")
@click.option("-q", "--quiet", is_flag=True, help="Suppress success output.")
@click.version_option(__version__, prog_name="freeconvert")
def main(input_path, output_path, src, dst, list_all, list_from, list_to, quiet):
    """Convert any file to any other format.

    \b
    Examples:
        freeconvert photo.png photo.jpg
        freeconvert resume.pdf resume.docx
        freeconvert song.flac --to mp3 -o song.mp3
        freeconvert --list-from pdf
    """
    if list_all:
        for s, d in supported_pairs():
            click.echo(f"{s:>10}  →  {d}")
        return
    if list_from:
        targets = sorted({d for (s, d) in supported_pairs() if s == list_from.lower()})
        if not targets:
            click.echo(f"No targets registered from {list_from!r}.", err=True)
            sys.exit(2)
        for d in targets:
            click.echo(d)
        return
    if list_to:
        sources = sorted({s for (s, d) in supported_pairs() if d == list_to.lower()})
        if not sources:
            click.echo(f"No sources registered → {list_to!r}.", err=True)
            sys.exit(2)
        for s in sources:
            click.echo(s)
        return

    if not input_path or not output_path:
        click.echo("Usage: freeconvert INPUT OUTPUT  (or --list / --list-from / --list-to)",
                   err=True)
        sys.exit(2)

    # Pre-flight: tell the user which handler will run before invoking it.
    resolved = lookup(src or input_path.suffix.lstrip("."),
                      dst or output_path.suffix.lstrip("."))
    if resolved is None:
        s = (src or input_path.suffix.lstrip(".")) or "?"
        d = (dst or output_path.suffix.lstrip(".")) or "?"
        click.echo(f"error: no handler registered for {s!r} → {d!r}", err=True)
        click.echo("       try `freeconvert --list-from {s}` to see available targets.".format(s=s),
                   err=True)
        sys.exit(2)

    try:
        info = convert(str(input_path), str(output_path), src=src, dst=dst)
    except UnsupportedConversionError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    except FileNotFoundError as e:
        click.echo(f"error: file not found: {e}", err=True)
        sys.exit(2)
    except RuntimeError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    if not quiet:
        click.echo(
            f"✓ {info['src']} → {info['dst']}  ({info['category']})  {output_path}"
        )


if __name__ == "__main__":
    main()
