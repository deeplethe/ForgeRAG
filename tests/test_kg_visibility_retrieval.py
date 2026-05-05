"""
KG visibility on the retrieval / answer path.

S5.3 closed the leak on ``/api/v1/graph/*`` (api/auth/kg_visibility +
api/routes/graph.py). The same leak — KG entity / relation
descriptions LLM-synthesised across every source chunk — also
existed on the retrieval path: ``retrieval/kg_path.py`` collects
descriptions into ``self.kg_context``, and ``answering/prompts.py``
inserts them directly into the LLM prompt.

Two pre-existing bugs converged here:

  1. The 4 sites that append entity / relation dicts to
     ``KGContext`` did not carry ``source_doc_ids`` on the dict.
  2. ``_scope_kg_context`` checks ``e.get("source_doc_ids")`` and
     drops on missing — so in multi-user mode the entire KG
     synthesis layer was silently disabled (zero entries surviving
     the filter).

The fix:

  * Populate ``source_doc_ids`` on every ``ctx.entities.append`` /
    ``ctx.relations.append``.
  * Rewrite ``_scope_kg_context`` with 3-tier visibility, but with
    a **stricter** policy than the API surface: partial AND hidden
    both drop. The API surface returns partial entries with
    ``description=null`` plus a ``visibility`` block — that's
    appropriate for a UI that can render the metadata. The LLM
    prompt is opaque text; a name-only entry is either useless to
    the model or actively risky (entity-existence leak,
    hallucination prompt).

These tests exercise:

  * The ``_scope_kg_context`` filter directly: full / partial /
    hidden / missing-sources.
  * The ``answering/prompts.py`` defensive None-description skip
    so the prompt renderer never crashes on a redacted entry.
"""

from __future__ import annotations

from answering.prompts import _estimate_kg_chars, _render_user_message
from config.retrieval import KGPathConfig
from retrieval.kg_path import KGPath
from retrieval.types import KGContext

# ---------------------------------------------------------------------------
# _scope_kg_context — direct filter behavior
# ---------------------------------------------------------------------------


def _kgpath() -> KGPath:
    """Build a KGPath with no real backends — we only exercise the
    filter, which doesn't touch graph or relational stores."""
    return KGPath(cfg=KGPathConfig(), graph=None, relational=None)


def _ent(eid: str, *, src_docs: list[str], desc: str = "synthesised") -> dict:
    return {
        "name": f"E{eid}",
        "type": "concept",
        "description": desc,
        "source_doc_ids": src_docs,
        "_eid": eid,
    }


def _rel(rid: str, *, src_docs: list[str], desc: str = "rel-syn") -> dict:
    return {
        "source": "left",
        "target": "right",
        "keywords": "",
        "description": desc,
        "source_doc_ids": src_docs,
        "_rid": rid,
    }


class TestScopeKGContextEntities:
    def test_full_kept(self):
        kg = _kgpath()
        kg.kg_context = KGContext(
            entities=[_ent("e1", src_docs=["d_a", "d_b"])],
        )
        kg._scope_kg_context({"d_a", "d_b"})
        assert len(kg.kg_context.entities) == 1
        assert kg.kg_context.entities[0]["_eid"] == "e1"

    def test_partial_dropped(self):
        """Stricter than API-surface S5.3: retrieval drops partial.
        LLM context is opaque; a name-only entry leaks existence and
        risks hallucination, with no visibility-banner UI to mediate."""
        kg = _kgpath()
        kg.kg_context = KGContext(
            entities=[_ent("e1", src_docs=["d_a", "d_b"])],
        )
        kg._scope_kg_context({"d_a"})  # only one of two sources
        assert kg.kg_context.entities == []

    def test_hidden_dropped(self):
        kg = _kgpath()
        kg.kg_context = KGContext(
            entities=[_ent("e1", src_docs=["d_x"])],
        )
        kg._scope_kg_context({"d_a"})
        assert kg.kg_context.entities == []

    def test_missing_source_doc_ids_dropped(self):
        """Defensive: any populate site that forgot to write
        source_doc_ids gets dropped rather than leaked. Multiple
        bugs landed here historically — fail closed."""
        kg = _kgpath()
        # Note: dict missing source_doc_ids entirely.
        kg.kg_context = KGContext(
            entities=[
                {
                    "name": "Orphan",
                    "type": "concept",
                    "description": "no provenance",
                    "_eid": "e1",
                }
            ],
        )
        kg._scope_kg_context({"d_a"})
        assert kg.kg_context.entities == []

    def test_mixed_full_partial_hidden(self):
        kg = _kgpath()
        kg.kg_context = KGContext(
            entities=[
                _ent("e_full", src_docs=["d_a"]),  # full
                _ent("e_partial", src_docs=["d_a", "d_x"]),  # partial
                _ent("e_hidden", src_docs=["d_x", "d_y"]),  # hidden
            ],
        )
        kg._scope_kg_context({"d_a"})
        survivors = {e["_eid"] for e in kg.kg_context.entities}
        assert survivors == {"e_full"}


class TestScopeKGContextRelations:
    def test_full_kept(self):
        kg = _kgpath()
        kg.kg_context = KGContext(
            relations=[_rel("r1", src_docs=["d_a"])],
        )
        kg._scope_kg_context({"d_a"})
        assert len(kg.kg_context.relations) == 1

    def test_partial_dropped(self):
        kg = _kgpath()
        kg.kg_context = KGContext(
            relations=[_rel("r1", src_docs=["d_a", "d_b"])],
        )
        kg._scope_kg_context({"d_a"})
        assert kg.kg_context.relations == []

    def test_missing_source_doc_ids_dropped(self):
        kg = _kgpath()
        kg.kg_context = KGContext(
            relations=[
                {
                    "source": "L",
                    "target": "R",
                    "description": "no provenance",
                    "_rid": "r1",
                }
            ],
        )
        kg._scope_kg_context({"d_a"})
        assert kg.kg_context.relations == []

    def test_legacy_single_value_source_doc_id(self):
        """Belt-and-suspenders: ``source_doc_ids`` was historically
        sometimes a bare string. Filter accepts the size-1 shape."""
        kg = _kgpath()
        kg.kg_context = KGContext(
            relations=[
                {
                    "source": "L",
                    "target": "R",
                    "description": "x",
                    "source_doc_ids": "d_a",  # bare string, not list
                    "_rid": "r1",
                }
            ],
        )
        kg._scope_kg_context({"d_a"})
        assert len(kg.kg_context.relations) == 1

        kg.kg_context = KGContext(
            relations=[
                {
                    "source": "L",
                    "target": "R",
                    "description": "x",
                    "source_doc_ids": "d_x",
                    "_rid": "r1",
                }
            ],
        )
        kg._scope_kg_context({"d_a"})
        assert kg.kg_context.relations == []


# ---------------------------------------------------------------------------
# answering/prompts.py — defensive None description skip
# ---------------------------------------------------------------------------


class TestPromptsRedactedEntrySkip:
    """When the visibility filter (or a future code path) leaves an
    entity / relation with no description, the prompt renderer must
    silently skip it — never crash, never emit a name-only line."""

    def _cfg(self):
        from config.answering import GeneratorConfig

        return GeneratorConfig()

    def test_entity_with_no_description_is_skipped_in_prompt(self):
        cfg = self._cfg()
        kg_context = KGContext(
            entities=[
                {"name": "Visible", "type": "concept", "description": "real text"},
                {"name": "Redacted", "type": "concept", "description": None},
                {"name": "Empty", "type": "concept"},  # no description key
            ]
        )
        rendered = _render_user_message("q", [], cfg, kg_context=kg_context)
        assert "Visible" in rendered
        assert "real text" in rendered
        assert "Redacted" not in rendered
        assert "Empty" not in rendered

    def test_relation_with_no_description_is_skipped(self):
        cfg = self._cfg()
        kg_context = KGContext(
            relations=[
                {
                    "source": "A",
                    "target": "B",
                    "description": "linked",
                    "keywords": "",
                },
                {
                    "source": "C",
                    "target": "D",
                    "description": None,
                    "keywords": "",
                },
            ]
        )
        rendered = _render_user_message("q", [], cfg, kg_context=kg_context)
        assert "A" in rendered
        assert "linked" in rendered
        # The redacted relation's endpoints should not appear in the
        # KG section even though their names are present elsewhere
        # they're not — verify the description isn't there.
        # (Use a substring that could only come from the redacted line.)
        assert "C → D" not in rendered

    def test_estimate_skips_redacted_entries(self):
        """Char-budget estimate must not reserve space for entries
        the renderer will skip — otherwise the chunk budget gets
        cut for context that never appears."""
        cfg = self._cfg()
        empty_ctx = KGContext()
        ctx_only_redacted = KGContext(
            entities=[
                {"name": "X", "type": "concept", "description": None},
            ],
            relations=[
                {
                    "source": "A",
                    "target": "B",
                    "description": None,
                    "keywords": "",
                },
            ],
        )
        # All entries redacted → estimate should match the empty
        # case (zero KG section emitted).
        assert _estimate_kg_chars(ctx_only_redacted, cfg) == _estimate_kg_chars(
            empty_ctx, cfg
        )


# ---------------------------------------------------------------------------
# Regression — the pre-fix behavior would silently empty kg_context
# ---------------------------------------------------------------------------


def test_regression_entries_with_source_doc_ids_now_survive():
    """Pre-fix bug: ``_scope_kg_context`` checked
    ``e.get("source_doc_ids")`` but the populate sites never wrote
    that key. So in multi-user mode every legitimate entry got
    dropped. This test proves the fix — entries written by the
    real populate path (now carrying source_doc_ids) DO survive."""
    kg = _kgpath()
    # Mimic what the new populate path now writes.
    kg.kg_context = KGContext(
        entities=[
            {
                "name": "E1",
                "type": "person",
                "description": "syn",
                "source_doc_ids": ["d_a"],
                "_eid": "e1",
            }
        ],
        relations=[
            {
                "source": "X",
                "target": "Y",
                "keywords": "",
                "description": "syn-rel",
                "source_doc_ids": ["d_a"],
                "_rid": "r1",
            }
        ],
    )
    kg._scope_kg_context({"d_a"})
    assert len(kg.kg_context.entities) == 1
    assert len(kg.kg_context.relations) == 1
