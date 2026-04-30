"""
Top-level ingestion orchestrator.

Implements the user-confirmed phase split:

    Phase A (upload):
        bytes/path -> FileStore -> file_id
        Never touches the parser. Produces a durable blob + files row
        regardless of what happens next. Safe to retry.

    Phase B (ingest):
        file_id -> materialize blob -> parser -> tree -> chunker
                -> IngestionWriter (relational + vectors) -> doc_id
        Parse/embed/write are atomic at the relational layer. On
        failure the file stays; the user can re-invoke ingest(file_id)
        without re-uploading.

    Convenience:
        `upload_and_ingest` does both in one call for simple scripts.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from embedder.base import Embedder
from parser.chunker import Chunker
from parser.pipeline import ParserPipeline
from parser.tree_builder import TreeBuilder
from persistence.files import FileSource, FileStore
from persistence.ingestion_writer import IngestionWriter
from persistence.store import Store
from persistence.vector.base import VectorStore

log = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    file_id: str
    doc_id: str | None = None
    parse_version: int = 1
    num_chunks: int = 0
    num_blocks: int = 0
    tree_quality: float = 0.0


class _DocumentDeleted(Exception):
    """Raised when document is deleted mid-pipeline to abort cleanly."""


@dataclass
class _ChunkView:
    """Duck-typed minimal chunk shape for ``_run_kg_extraction``.

    The KG extractor only reads ``chunk_id`` + ``content`` from chunk
    objects. ``run_kg_for_doc`` reloads chunks from the store as dict
    rows, then wraps each in this lightweight container so the existing
    extraction code path doesn't have to branch on input shape.

    ``role`` is included so the role-based KG skip filter
    (``_SKIP_ROLES`` in ``_run_kg_extraction``) sees the same field as
    a real ``Chunk``. Defaults to ``"main"`` for callers that omit it
    (e.g. legacy code paths reloading chunks before the role column
    existed in storage).
    """

    chunk_id: str
    content: str
    role: str = "main"


class IngestionPipeline:
    def __init__(
        self,
        *,
        file_store: FileStore,
        parser: ParserPipeline,
        tree_builder: TreeBuilder,
        chunker: Chunker,
        relational_store: Store,
        vector_store: VectorStore | None = None,
        embedder: Embedder | None = None,
        graph_store=None,  # Optional[GraphStore]
        kg_extraction_cfg=None,  # Optional[KGExtractionConfig]
        kg_submit: Callable[[str], None] | None = None,
    ):
        self.files = file_store
        self.parser = parser
        self.tree_builder = tree_builder
        self.chunker = chunker
        self.rel = relational_store
        self.vec = vector_store
        self.embedder = embedder
        self.graph_store = graph_store
        self.kg_extraction_cfg = kg_extraction_cfg
        # Submit a doc_id to the dedicated KG-extraction worker pool.
        # When set, ``ingest()`` returns immediately after the embed phase
        # commits and the KG step runs asynchronously on the kg pool. When
        # None (e.g. tests), KG runs inline on the calling thread.
        self.kg_submit = kg_submit
        # Per-doc serialization with refcount: popping after release would
        # let a third concurrent caller arriving between pop and the second
        # waiter's acquire create a fresh lock and bypass serialization.
        self._doc_locks: dict[str, list] = {}  # doc_id → [Lock, refcount]
        self._lock_lock = threading.Lock()

    def _assert_not_deleted(self, doc_id: str) -> None:
        """Raise _DocumentDeleted if the document was removed mid-pipeline."""
        if self.rel.get_document(doc_id) is None:
            raise _DocumentDeleted(doc_id)

    # =====================================================================
    # Phase A: file upload only
    # =====================================================================

    def upload(
        self,
        source: FileSource,
        *,
        original_name: str,
        mime_type: str | None = None,
    ) -> str:
        record = self.files.store(
            source,
            original_name=original_name,
            mime_type=mime_type,
        )
        log.info("phase-A upload done file_id=%s", record["file_id"])
        return record["file_id"]

    # =====================================================================
    # Phase B: parse + index an already-uploaded file
    # =====================================================================

    def ingest(
        self,
        file_id: str,
        *,
        doc_id: str | None = None,
        parse_version: int = 1,
        enrich_summary: bool | None = None,
        force_reparse: bool = False,
    ) -> IngestionResult:
        file_row = self.rel.get_file(file_id)
        if not file_row:
            raise KeyError(f"file {file_id} not found")

        if doc_id is None:
            doc_id = f"doc_{file_id[:12]}"

        # Ensure document placeholder exists (may already be created by API route).
        # Use ``original_name`` (the user's filename) — not ``display_name``,
        # which is the ``<stem>_<ts>_<rand>`` internal blob name. Falling
        # back to display_name keeps old code paths alive in case
        # original_name is ever absent.
        original_name = file_row.get("original_name") or file_row["display_name"]
        self.rel.create_document_placeholder(
            doc_id=doc_id,
            file_id=file_id,
            filename=original_name,
            format=Path(original_name).suffix.lstrip(".") or "bin",
            status="pending",
        )

        with self._lock_lock:
            entry = self._doc_locks.get(doc_id)
            if entry is None:
                entry = [threading.Lock(), 0]
                self._doc_locks[doc_id] = entry
            entry[1] += 1
            doc_lock = entry[0]
        doc_lock.acquire()
        try:
            return self._ingest_inner(
                file_id,
                file_row,
                doc_id=doc_id,
                parse_version=parse_version,
                enrich_summary=enrich_summary,
                force_reparse=force_reparse,
            )
        finally:
            doc_lock.release()
            with self._lock_lock:
                entry = self._doc_locks.get(doc_id)
                if entry is not None:
                    entry[1] -= 1
                    if entry[1] <= 0:
                        self._doc_locks.pop(doc_id, None)

    def _ingest_inner(
        self,
        file_id: str,
        file_row: dict,
        *,
        doc_id: str,
        parse_version: int = 1,
        enrich_summary: bool | None = None,
        force_reparse: bool = False,
    ) -> IngestionResult:
        # Handle force_reparse: auto-bump parse_version
        if force_reparse:
            existing = self.rel.get_document(doc_id)
            if existing:
                parse_version = existing["active_parse_version"] + 1
                log.info("force_reparse: bumping %s to version %d", doc_id, parse_version)

        # Resolve summary enrichment:
        # - explicit enrich_summary param takes priority
        # - tree builder LLM enabled → auto enrich
        do_summary = enrich_summary
        if do_summary is None:
            do_summary = self.tree_builder.cfg.llm_enabled

        ext = Path(file_row["display_name"]).suffix or ".bin"
        tmp_dir = Path("./storage/tmp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="qr_parse_",
            suffix=ext,
            delete=False,
            dir=str(tmp_dir),
        ) as tmp:
            local_path = tmp.name
        original_path = local_path  # keep for cleanup
        converted_pdf_path: str | None = None
        try:
            self.files.materialize(file_id, local_path)

            from datetime import datetime

            # ── Phase 0: Convert non-PDF to PDF if needed ──
            from ingestion.converter import convert_to_pdf, needs_conversion

            if needs_conversion(local_path):
                self.rel.update_document_status(doc_id, status="converting")
                log.info("converting %s to PDF for parsing", Path(local_path).suffix)
                try:
                    converted_pdf_path = str(convert_to_pdf(local_path))
                    # Store the converted PDF as a new file so frontend can view it
                    pdf_file_id = self._store_converted_pdf(
                        converted_pdf_path,
                        file_row,
                    )
                    self.rel.update_document_status(doc_id, pdf_file_id=pdf_file_id)
                    # Parse the PDF instead of the original
                    local_path = converted_pdf_path
                except Exception as e:
                    log.error("conversion failed: %s", e, exc_info=True)
                    raise RuntimeError(f"Failed to convert {Path(file_row['display_name']).suffix} to PDF: {e}") from e

            # ── Phase 1: Parse (pure extraction, no LLM) ──
            self.rel.update_document_status(doc_id, status="parsing", parse_started_at=datetime.utcnow())
            parsed = self.parser.parse(local_path, doc_id=doc_id, parse_version=parse_version)
            # Fix filename: parser sets the temp file name, but we want
            # the user-visible original filename. ``display_name`` is the
            # internal ``<stem>_<ts>_<rand>`` blob name and must NOT be
            # exposed here — that gets written through to Document.filename
            # on the next ``upsert_document`` call.
            parsed.filename = file_row.get("original_name") or file_row["display_name"]

            # For text / markdown files: the PDF conversion renders
            # Markdown headings at different font sizes, but PyMuPDF
            # may still classify them as paragraphs.  A lightweight
            # second pass re-tags blocks whose text matches ``# heading``
            # or ``**heading**`` patterns so the tree builder can use
            # the heading-based strategy instead of falling back to flat.
            fmt = Path(parsed.filename).suffix.lower().lstrip(".")
            if fmt in ("text", "md", "txt", "markdown"):
                from ingestion.md_headings import reclassify_md_headings

                reclassify_md_headings(parsed)

            self.rel.update_document_status(doc_id, status="parsed", parse_completed_at=datetime.utcnow())

            # ── Phase 2: Tree structure (may call LLM) ──
            self.rel.update_document_status(doc_id, status="structuring", structure_started_at=datetime.utcnow())
            tree = self.tree_builder.build(parsed)
            self.rel.update_document_status(doc_id, structure_completed_at=datetime.utcnow())

            # Record tree navigation eligibility
            tree_navigable = (
                tree.quality_score >= 0.4
                and tree.generation_method != "fallback"
                and len([n for n in tree.nodes.values() if not n.children]) >= 3
            )
            self.rel.update_document_status(
                doc_id,
                tree_navigable=tree_navigable,
                tree_quality=tree.quality_score,
                tree_method=tree.generation_method,
            )

            self._assert_not_deleted(doc_id)

            # ── Phase 3: LLM enrichment (VLM images + summaries) ──
            # MUST run BEFORE chunking so enriched block.text / tree
            # summaries are captured into chunk content for embedding.
            # Note: page_groups strategy already produces summaries during
            # tree building, so skip summary enrichment for those trees.
            image_count = 0
            summary_count = 0
            tree_has_summaries = tree.generation_method == "page_groups"
            if do_summary:
                # provider_id audit column kept for legacy rows; new ingests
                # only record the model id (each module owns its own).
                enrich_provider_id = None
                enrich_model = getattr(self.parser.cfg.image_enrichment, "model", None)
                self.rel.update_document_status(doc_id, enrich_status="running", enrich_started_at=datetime.utcnow())
                image_count = self._enrich_images(parsed)
                if image_count:
                    log.info("enriched %d images with VLM descriptions", image_count)
                if self.embedder is not None and not tree_has_summaries:
                    summary_count = self._enrich_summaries(tree, parsed)
                elif tree_has_summaries:
                    summary_count = sum(1 for n in tree.nodes.values() if n.summary)
                    log.info("page_groups tree already has %d summaries, skipping enrichment", summary_count)
                if image_count < 0 or summary_count < 0:
                    enrich_status = "partial"
                else:
                    enrich_status = "done"
                self.rel.update_document_status(
                    doc_id,
                    enrich_status=enrich_status,
                    enrich_provider_id=enrich_provider_id,
                    enrich_model=enrich_model,
                    enrich_summary_count=max(summary_count, 0),
                    enrich_image_count=max(image_count, 0),
                    enrich_at=datetime.utcnow(),
                )
            else:
                # Even without LLM enrichment, ensure cheap summaries for navigation
                self._ensure_cheap_summaries(tree, parsed)
                self.rel.update_document_status(doc_id, enrich_status="skipped")

            # ── Phase 4: Chunk (after enrichment, so chunks contain
            #    VLM image descriptions + enriched text) ──
            chunks = self.chunker.chunk(parsed, tree)

            self._assert_not_deleted(doc_id)

            # ── Phase 5: Write relational + embed ──
            # Chunks must be persisted BEFORE KG extraction so the graph
            # store never references chunk_ids that don't exist in the
            # relational store. IngestionWriter wraps blocks/tree/chunks
            # in a single relational transaction.
            embed_provider_id = None
            embed_model = getattr(getattr(self.parser.cfg.embedder, "litellm", None), "model", None)
            self.rel.update_document_status(
                doc_id, status="embedding", embed_status="running", embed_started_at=datetime.utcnow()
            )

            writer = IngestionWriter(self.rel, vector=self.vec, embedder=self.embedder)
            writer.write(parsed, tree, chunks, file_id=file_id)

            self.rel.update_document_status(
                doc_id,
                status="ready",
                embed_status="done",
                embed_provider_id=embed_provider_id,
                embed_model=embed_model,
                embed_at=datetime.utcnow(),
            )

            # ── Phase 6: Knowledge Graph extraction ──
            # Runs AFTER chunks are committed so the graph never references
            # chunk_ids that don't exist in the relational store. Failure
            # leaves the doc queryable via BM25/vector/tree with
            # kg_status="error" so the user can retry KG only.
            #
            # When a dedicated KG worker pool is wired in (production path
            # via api.state), we hand off the doc_id to that pool and the
            # current ``ingest`` worker returns immediately — keeping the
            # parse/embed pool latency-bounded under concurrent uploads.
            # Without ``kg_submit`` (tests / single-process callers) KG
            # runs inline on the calling thread for backward compatibility.
            if self.kg_submit is not None:
                # Mark queued so polling clients can distinguish "embed
                # done, KG pending in queue" from "embed done, KG never
                # configured (skipped)".
                with contextlib.suppress(Exception):
                    self.rel.update_document_status(doc_id, kg_status="queued")
                self.kg_submit(doc_id)
            else:
                self._run_kg_extraction(doc_id, chunks)

            log.info(
                "phase-B ingest done doc_id=%s file_id=%s blocks=%d chunks=%d summaries=%d",
                doc_id,
                file_id,
                len(parsed.blocks),
                len(chunks),
                summary_count,
            )
            return IngestionResult(
                file_id=file_id,
                doc_id=doc_id,
                parse_version=parse_version,
                num_chunks=len(chunks),
                num_blocks=len(parsed.blocks),
                tree_quality=tree.quality_score,
            )
        except _DocumentDeleted:
            log.warning(
                "document %s deleted mid-pipeline — aborting cleanly",
                doc_id,
            )
            return IngestionResult(file_id=file_id, doc_id=doc_id)
        except Exception as exc:
            try:
                msg = str(exc)[:500] if str(exc) else type(exc).__name__
                self.rel.update_document_status(
                    doc_id,
                    status="error",
                    error_message=msg,
                )
            except Exception:
                pass
            raise
        finally:
            # Clean up original temp file (DOCX/PPTX/etc.)
            with contextlib.suppress(OSError):
                Path(original_path).unlink()
            # Clean up converted PDF temp file (if different from original)
            if converted_pdf_path and converted_pdf_path != original_path:
                with contextlib.suppress(OSError):
                    Path(converted_pdf_path).unlink()

    # =====================================================================
    # Convenience
    # =====================================================================

    # ==================================================================
    # Internal helpers
    # ==================================================================

    def _store_converted_pdf(self, pdf_path: str, original_file_row: dict) -> str:
        """
        Store a converted PDF as a new file in the FileStore.
        Returns the new file_id for the PDF.
        """
        orig_name = original_file_row["display_name"]
        stem = Path(orig_name).stem
        pdf_name = f"{stem}.pdf"
        record = self.files.store(
            Path(pdf_path),
            original_name=pdf_name,
            mime_type="application/pdf",
        )
        log.info(
            "stored converted PDF: file_id=%s (%s → %s)",
            record["file_id"],
            orig_name,
            pdf_name,
        )
        return record["file_id"]

    def run_kg_for_doc(self, doc_id: str) -> None:
        """KG worker entry point.

        Reloads chunks for *doc_id* from the relational store (they were
        committed during the embed phase) and runs KG extraction +
        consolidation against them. This is called from the dedicated
        KG worker pool, not the parse/embed pool.

        Failures are captured into ``kg_status="error"`` with a short
        ``error_message``; the doc remains fully queryable via the
        non-KG retrieval paths in the meantime.
        """
        doc = self.rel.get_document(doc_id)
        if doc is None:
            log.warning("KG worker: doc %s not found (deleted?)", doc_id)
            return
        parse_version = doc.get("active_parse_version") or 1
        rows = self.rel.get_chunks(doc_id, parse_version)
        # Duck-typed view: ``_run_kg_extraction`` only reads ``.chunk_id``
        # and ``.content`` from each chunk.
        chunks = [
            _ChunkView(r["chunk_id"], r.get("content") or "", r.get("role") or "main")
            for r in rows
        ]
        self._run_kg_extraction(doc_id, chunks)

    def _run_kg_extraction(self, doc_id: str, chunks: list) -> None:
        """
        Extract and persist KG entities/relations for *doc_id* against the
        already-committed chunks.

        Best-effort: any failure (extraction, embedding, graph upsert,
        deletion mid-flight) is captured into kg_status="error" with a
        short error_message, and any partially-written graph entries for
        this doc are rolled back via delete_by_doc(). The doc remains
        queryable through BM25/vector/tree paths.
        """
        if self.graph_store is None:
            return

        kg_cfg = self.kg_extraction_cfg
        if kg_cfg is None:
            # No KG extraction config wired in (e.g. test environment).
            self.rel.update_document_status(doc_id, kg_status="skipped")
            return

        # Re-ingest safety: clean up old graph entries for this doc.
        try:
            removed = self.graph_store.delete_by_doc(doc_id)
            if removed:
                log.info("cleaned up %d old graph entries for doc %s", removed, doc_id)
        except Exception as e:
            log.warning("graph cleanup failed for %s: %s", doc_id, e)

        from datetime import datetime

        self.rel.update_document_status(
            doc_id,
            kg_status="running",
            kg_started_at=datetime.utcnow(),
        )
        log.info(
            "KG extraction starting: model=%s api_base=%s api_key=%s",
            kg_cfg.model,
            kg_cfg.api_base,
            ("***" + kg_cfg.api_key[-4:]) if kg_cfg.api_key else "NONE",
        )

        try:
            from ingestion.kg_extractor import KGExtractor

            extractor = KGExtractor(
                model=kg_cfg.model,
                api_key=kg_cfg.api_key,
                api_key_env=kg_cfg.api_key_env,
                api_base=kg_cfg.api_base,
                timeout=kg_cfg.timeout,
            )
            # Threading the owning document's path into each chunk dict
            # lets the extractor denormalize path onto Entity/Relation.source_paths.
            doc_row = self.rel.get_document(doc_id) or {}
            doc_path = doc_row.get("path") or "/"
            # Skip noise sources by ``chunk.role`` — Index, TOC, and
            # Bibliography consistently produce placeholder entities
            # (page numbers, lone author names, title-only references)
            # without real relations. Glossary, Appendix, and
            # Front matter STAY in: definitions, supplementary prose,
            # and Foreword/Preface bodies routinely carry real entities
            # (authors, organizations, themes). Copyright/dedication
            # pages are short enough that letting them through costs
            # nothing. ``main`` is the default for body chapters.
            _SKIP_ROLES = {"toc", "index", "bibliography"}
            kg_chunks = [c for c in chunks if (c.role or "main") not in _SKIP_ROLES]
            skipped = len(chunks) - len(kg_chunks)
            if skipped:
                log.info(
                    "KG extraction: skipped %d chunks by role (TOC/Index/Bibliography)",
                    skipped,
                )
            chunk_dicts = [{"chunk_id": c.chunk_id, "content": c.content, "path": doc_path} for c in kg_chunks]
            entities, relations = extractor.extract_batch(
                chunk_dicts,
                doc_id,
                max_workers=kg_cfg.max_workers,
            )

            frag_thresh = getattr(kg_cfg, "merge_description_threshold", 6)
            char_thresh = getattr(kg_cfg, "merge_description_max_chars", 2000)
            if frag_thresh > 0:
                from ingestion.kg_extractor import consolidate_descriptions

                ent_m, rel_m = consolidate_descriptions(
                    entities,
                    relations,
                    model=kg_cfg.model,
                    api_key=kg_cfg.api_key,
                    api_base=kg_cfg.api_base,
                    timeout=kg_cfg.timeout,
                    fragment_threshold=frag_thresh,
                    char_threshold=char_thresh,
                    max_workers=kg_cfg.max_workers,
                )
                if ent_m or rel_m:
                    log.info(
                        "KG description merge: %d entities, %d relations consolidated",
                        ent_m,
                        rel_m,
                    )

            # Always embed entity names + relation descriptions when an
            # embedder is available — the dependent retrieval features
            # (entity_disambiguation, KGPath relation-semantic search)
            # silently degrade without these vectors.
            if self.embedder is not None:
                ents_to_embed = [e for e in entities if not e.name_embedding]
                if ents_to_embed:
                    try:
                        embs = self.embedder.embed_texts([e.name for e in ents_to_embed])
                        for e, emb in zip(ents_to_embed, embs, strict=False):
                            e.name_embedding = emb
                    except Exception:
                        log.warning("Entity name embedding failed for %s", doc_id)

                rels_to_embed = [r for r in relations if r.description and not r.description_embedding]
                if rels_to_embed:
                    try:
                        embs = self.embedder.embed_texts([r.description for r in rels_to_embed])
                        for r, emb in zip(rels_to_embed, embs, strict=False):
                            r.description_embedding = emb
                    except Exception:
                        log.warning("Relation embedding failed for %s", doc_id)

            self._assert_not_deleted(doc_id)
            # Wrap all graph mutations for this doc in a single batch so
            # file-backed backends (NetworkX) defer the on-disk JSON
            # rewrite to one atomic dump at the end instead of N dumps
            # per upsert. For 169-chunk docs that's ~3000 fewer full-graph
            # writes — the dominant cost saving in this commit.
            self.graph_store.begin_batch()
            try:
                for e in entities:
                    self.graph_store.upsert_entity(e)
                for r in relations:
                    self.graph_store.upsert_relation(r)

                if frag_thresh > 0:
                    self._consolidate_graph_descriptions(
                        entities,
                        kg_cfg,
                        frag_thresh=frag_thresh,
                        char_thresh=char_thresh,
                    )
            finally:
                self.graph_store.end_batch()

            self.rel.update_document_status(
                doc_id,
                kg_status="done",
                kg_entity_count=len(entities),
                kg_relation_count=len(relations),
                kg_completed_at=datetime.utcnow(),
                kg_provider_id=None,
                kg_model=kg_cfg.model,
            )
            log.info(
                "KG extraction done doc_id=%s entities=%d relations=%d",
                doc_id,
                len(entities),
                len(relations),
            )
        except _DocumentDeleted:
            # Doc deleted mid-extraction — purge any partial graph state.
            with contextlib.suppress(Exception):
                self.graph_store.delete_by_doc(doc_id)
            raise
        except Exception as e:
            log.warning("KG extraction failed for %s: %s", doc_id, e, exc_info=True)
            # Rollback partial entities/relations so a future retry starts clean.
            with contextlib.suppress(Exception):
                self.graph_store.delete_by_doc(doc_id)
            err_msg = str(e)[:500] if str(e) else type(e).__name__
            with contextlib.suppress(Exception):
                self.rel.update_document_status(
                    doc_id,
                    kg_status="error",
                    error_message=f"KG: {err_msg}",
                )

    def _consolidate_graph_descriptions(
        self,
        entities: list,
        kg_cfg,
        *,
        frag_thresh: int = 6,
        char_thresh: int = 2000,
    ) -> None:
        """Re-read entities from graph store after upsert and consolidate
        descriptions that have grown too long from cross-document merges."""
        if self.graph_store is None:
            return
        from ingestion.kg_extractor import _count_fragments, consolidate_descriptions

        needs_merge = []
        for e in entities:
            stored = self.graph_store.get_entity(e.entity_id)
            if stored is None:
                continue
            if _count_fragments(stored.description) >= frag_thresh or len(stored.description) >= char_thresh:
                needs_merge.append(stored)

        if not needs_merge:
            return

        ent_m, _ = consolidate_descriptions(
            needs_merge,
            [],
            model=kg_cfg.model,
            api_key=kg_cfg.api_key,
            api_base=kg_cfg.api_base,
            timeout=kg_cfg.timeout,
            fragment_threshold=frag_thresh,
            char_threshold=char_thresh,
            max_workers=kg_cfg.max_workers,
        )
        # Write back consolidated descriptions via dedicated method
        # (avoids double-append that upsert_entity would cause)
        if ent_m > 0:
            for e in needs_merge:
                self.graph_store.update_entity_description(
                    e.entity_id,
                    e.description,
                )
            log.info(
                "cross-document description merge: %d/%d entities consolidated",
                ent_m,
                len(needs_merge),
            )

    def _enrich_summaries(self, tree, parsed) -> int:
        """Run LLM summary enrichment using batch mode (fewer API calls)."""
        try:
            from parser.summary import batch_enrich_tree_summaries, make_summary_fn

            gen_cfg = self.parser.cfg.answering.generator
            summary_fn = make_summary_fn(
                model=gen_cfg.model,
                api_key=gen_cfg.api_key,
                api_key_env=gen_cfg.api_key_env,
                api_base=gen_cfg.api_base,
            )
            count, failures = batch_enrich_tree_summaries(
                tree,
                parsed,
                generate_fn=summary_fn,
                batch_size=8,
            )
            if failures > 0:
                log.info(
                    "batch summary: %d succeeded, %d failed (cheap fallback applied)",
                    count,
                    failures,
                )
            return count
        except Exception as e:
            log.warning("summary enrichment failed: %s; applying cheap fallback", e)
            self._ensure_cheap_summaries(tree, parsed)
            return -1

    def _ensure_cheap_summaries(self, tree, parsed) -> None:
        """Fill empty summaries with cheap text-extraction fallback."""
        from parser.summary import cheap_node_summary

        blocks_index = parsed.blocks_by_id()
        count = 0
        for node in tree.walk_preorder():
            if node.summary:
                continue
            node.summary = cheap_node_summary(node, blocks_index)
            if node.summary:
                count += 1
        if count:
            log.info("cheap summary fallback: filled %d nodes", count)

    def _enrich_images(self, parsed) -> int:
        """
        Run VLM image description + OCR on figure blocks.
        Uses the image_enrichment config for the VLM call.
        """
        try:
            from parser.image_enrichment import enrich_images, make_vlm_fn

            img_cfg = self.parser.cfg.image_enrichment
            vlm_fn = make_vlm_fn(
                model=img_cfg.model,
                api_key=img_cfg.api_key,
                api_key_env=img_cfg.api_key_env,
                api_base=img_cfg.api_base,
            )
            from parser.blob_store import make_blob_store

            blob = make_blob_store(self.parser.cfg.storage.to_dataclass())
            count, _ = enrich_images(
                parsed,
                blob,
                vlm_fn=vlm_fn,
                max_workers=img_cfg.max_workers,
            )
            return count
        except Exception as e:
            log.warning("image enrichment failed: %s", e)
            return -1

    # ==================================================================
    # Convenience
    # ==================================================================

    def upload_and_ingest(
        self,
        source: FileSource,
        *,
        original_name: str,
        mime_type: str | None = None,
        doc_id: str | None = None,
        parse_version: int = 1,
        enrich_summary: bool | None = None,
    ) -> IngestionResult:
        file_id = self.upload(source, original_name=original_name, mime_type=mime_type)
        return self.ingest(
            file_id,
            doc_id=doc_id,
            parse_version=parse_version,
            enrich_summary=enrich_summary,
        )
