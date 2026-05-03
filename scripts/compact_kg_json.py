"""One-shot rewrite of an existing kg.json to compact format.

Use after upgrading to the compact ``_save_locked`` writer if you don't
want to wait for the next KG mutation to trigger the rewrite naturally.
Reads with ijson (one entity / relation at a time) and writes with
``json.dumps(..., separators=(',',':'))`` per item, so peak memory is
bounded to a single entity / relation regardless of input size.

Usage::

    .venv\\Scripts\\python.exe scripts\\compact_kg_json.py
    .venv\\Scripts\\python.exe scripts\\compact_kg_json.py --src storage/kg.json --dry-run

Atomic: writes ``<src>.compact.tmp`` first, then renames to ``<src>``
only if no error fired. Original is preserved until rename. Run with the
backend STOPPED so the rename can replace the file (Windows holds locks
on open files).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default="storage/kg.json", type=Path, help="kg.json path")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Write to <src>.compact.tmp but don't rename",
    )
    args = ap.parse_args()

    src: Path = args.src
    if not src.exists():
        print(f"ERROR: {src} does not exist", file=sys.stderr)
        return 1

    try:
        import ijson
    except ImportError:
        print(
            "ERROR: ijson not installed. pip install ijson",
            file=sys.stderr,
        )
        return 1

    src_size = src.stat().st_size
    print(f"source : {src} ({src_size / 1e9:.2f} GB)")

    tmp = src.with_suffix(".compact.tmp")
    if tmp.exists():
        tmp.unlink()
    print(f"target : {tmp}")

    compact = (",", ":")  # no spaces between dict / list items
    n_nodes = 0
    n_edges = 0
    t0 = time.time()
    last_log = t0

    # Pass 1: nodes
    with open(src, "rb") as fh, open(tmp, "w", encoding="utf-8") as out:
        out.write('{"nodes":[')
        first = True
        # ``use_float=True`` decodes JSON numbers as Python floats
        # instead of Decimal — json.dump can't serialise Decimal, and
        # entity name_embedding arrays are full of them.
        for nd in ijson.items(fh, "nodes.item", use_float=True):
            out.write("\n" if first else ",\n")
            json.dump(nd, out, ensure_ascii=False, separators=compact)
            first = False
            n_nodes += 1
            now = time.time()
            if now - last_log > 5:
                print(
                    f"  [{now - t0:5.1f}s] {n_nodes:>6} nodes processed "
                    f"({n_nodes / (now - t0):.0f}/s)",
                    flush=True,
                )
                last_log = now
        out.write('\n],"edges":[')

        # Pass 2: edges (rewind input)
        fh.seek(0)
        first = True
        for ed in ijson.items(fh, "edges.item", use_float=True):
            out.write("\n" if first else ",\n")
            json.dump(ed, out, ensure_ascii=False, separators=compact)
            first = False
            n_edges += 1
            now = time.time()
            if now - last_log > 5:
                print(
                    f"  [{now - t0:5.1f}s] {n_edges:>6} edges processed "
                    f"({n_edges / (now - t0):.0f}/s)",
                    flush=True,
                )
                last_log = now
        out.write("\n]}\n")

    elapsed = time.time() - t0
    out_size = tmp.stat().st_size
    saved = src_size - out_size
    print()
    print(f"done in {elapsed:.1f}s")
    print(f"  nodes      : {n_nodes:,}")
    print(f"  edges      : {n_edges:,}")
    print(f"  source     : {src_size / 1e9:.3f} GB")
    print(f"  compact    : {out_size / 1e9:.3f} GB")
    print(f"  saved      : {saved / 1e9:.3f} GB ({100 * saved / src_size:.1f}%)")

    if args.dry_run:
        print()
        print(f"--dry-run: kept {tmp}, original untouched.")
        return 0

    print()
    print(f"renaming {tmp} -> {src} ...")
    # On Windows, target must not be open (backend holds the file).
    # Path.replace is atomic on POSIX and "best effort" on Windows.
    try:
        tmp.replace(src)
    except OSError as e:
        print(
            f"ERROR: rename failed ({e}). Backend probably has {src} open. "
            f"Stop the backend and re-run, or rename manually.",
            file=sys.stderr,
        )
        return 2
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
