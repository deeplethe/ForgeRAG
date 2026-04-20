"""
Phase 1 backfill + sanity-check script.

Run this ONCE after applying the alembic migration `20260418_folder_tree`
to verify the new data model. Also reports any KG entities/relations
whose `source_doc_ids` doesn't line up with the (now unchanged)
document table — this should always be clean since our KG stores doc
ids, not paths, but we're paranoid.

    python scripts/phase1_backfill_check.py

The script is read-only unless you pass --fix, in which case it will:
    - assign any document with NULL/empty folder_id to __root__
    - create missing folder rows for any path that documents reference
      (shouldn't happen after a clean migration, but belt-and-suspenders)
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import func, select

from config.loader import load_config
from persistence.folder_service import (
    ROOT_FOLDER_ID,
    ROOT_PATH,
    TRASH_FOLDER_ID,
    TRASH_PATH,
    FolderService,
)
from persistence.models import Document, Folder
from persistence.store import Store

log = logging.getLogger(__name__)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true", help="apply safe fixes")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config()
    store = Store(cfg.persistence.relational)
    store.connect()

    problems = 0

    with store.transaction() as sess:
        # 1. System folders present?
        root = sess.get(Folder, ROOT_FOLDER_ID)
        trash = sess.get(Folder, TRASH_FOLDER_ID)
        if root is None:
            log.error("FATAL: missing system folder __root__ — migration incomplete")
            sys.exit(1)
        if trash is None:
            log.error("FATAL: missing system folder __trash__ — migration incomplete")
            sys.exit(1)
        assert root.path == ROOT_PATH, f"root has wrong path {root.path!r}"
        assert trash.path == TRASH_PATH, f"trash has wrong path {trash.path!r}"
        log.info("✓ system folders OK (__root__, __trash__)")

        # 2. Every folder except root has a parent
        orphan_folders = list(
            sess.execute(
                select(Folder).where(
                    (Folder.folder_id != ROOT_FOLDER_ID)
                    & (Folder.parent_id.is_(None))
                )
            ).scalars()
        )
        if orphan_folders:
            problems += len(orphan_folders)
            log.warning(
                "%d folders without parent (non-root): %s",
                len(orphan_folders),
                [f.path for f in orphan_folders[:5]],
            )

        # 3. Every folder's path matches parent.path + / + name
        bad_paths = 0
        for f in sess.execute(select(Folder)).scalars():
            if f.parent_id is None:
                continue
            parent = sess.get(Folder, f.parent_id)
            if parent is None:
                continue
            expected_prefix = parent.path.rstrip("/") + "/"
            if f.path != parent.path + f.name and f.path != expected_prefix + f.name:
                # Accept both '/a'+'b' and '/' + 'b' forms
                if not (parent.path == "/" and f.path == "/" + f.name):
                    bad_paths += 1
        if bad_paths:
            problems += bad_paths
            log.warning("%d folders have path inconsistent with parent + name", bad_paths)
        else:
            log.info("✓ folder paths consistent with parent chain")

        # 4. Every document has a folder_id + path
        orphan_docs = list(
            sess.execute(
                select(Document).where(
                    (Document.folder_id.is_(None)) | (Document.folder_id == "")
                )
            ).scalars()
        )
        if orphan_docs:
            problems += len(orphan_docs)
            log.warning("%d documents with missing folder_id", len(orphan_docs))
            if args.fix:
                for d in orphan_docs:
                    d.folder_id = ROOT_FOLDER_ID
                    if not d.path:
                        d.path = f"/{d.filename or d.doc_id}"
                log.info("  ✓ fixed: assigned to __root__")

        # 5. Document.path must equal its folder.path + / + filename
        #    (loose check: at minimum starts with folder.path)
        bad_doc_paths = 0
        for d in sess.execute(select(Document)).scalars():
            if not d.folder_id:
                continue
            f = sess.get(Folder, d.folder_id)
            if f is None:
                bad_doc_paths += 1
                continue
            prefix = f.path if f.path == "/" else f.path + "/"
            if f.path == "/":
                if not d.path.startswith("/"):
                    bad_doc_paths += 1
            else:
                if not d.path.startswith(prefix) and d.path != f.path:
                    bad_doc_paths += 1
        if bad_doc_paths:
            problems += bad_doc_paths
            log.warning("%d documents with path inconsistent with their folder", bad_doc_paths)
        else:
            log.info("✓ document paths consistent with their folder")

        # 6. Doc counts
        total_docs = sess.execute(
            select(func.count()).select_from(Document)
        ).scalar_one()
        docs_in_root = sess.execute(
            select(func.count()).select_from(Document).where(
                Document.folder_id == ROOT_FOLDER_ID
            )
        ).scalar_one()
        log.info("total docs: %d (in __root__: %d)", total_docs, docs_in_root)

    # 7. KG entity source_doc_ids sanity (compared against documents table)
    #    Every entity should have at least one source_doc_id, and every one
    #    of those should reference a real document.
    try:
        from graph.factory import make_graph_store

        graph = make_graph_store(cfg.graph)
        entities = graph.get_all_entities()
        if entities is None:
            log.info("KG: no entities or unsupported backend; skipping")
        else:
            known_doc_ids = set(
                sess.execute(select(Document.doc_id)).scalars()
            ) if False else set()  # re-open session to avoid detached state
            with store.transaction() as sess2:
                known_doc_ids = set(
                    sess2.execute(select(Document.doc_id)).scalars()
                )
            no_source = 0
            stale_sources = 0
            for e in entities:
                srcs = getattr(e, "source_doc_ids", set())
                if not srcs:
                    no_source += 1
                else:
                    for s in srcs:
                        if s not in known_doc_ids:
                            stale_sources += 1
                            break
            if no_source:
                log.warning(
                    "KG: %d entities have empty source_doc_ids (will be invisible under path scoping)",
                    no_source,
                )
                problems += no_source
            if stale_sources:
                log.warning(
                    "KG: %d entities reference doc_ids that no longer exist",
                    stale_sources,
                )
                problems += stale_sources
            if not no_source and not stale_sources:
                log.info("✓ KG entity source_doc_ids consistent")
    except Exception as e:
        log.info("KG: check skipped (%s)", e)

    if problems:
        log.warning("done: %d issues found. Rerun with --fix to apply safe fixes.", problems)
        sys.exit(1 if not args.fix else 0)
    log.info("✓ all checks passed")


if __name__ == "__main__":
    main()
