"""Integration tests for ``IngestionPipeline._summarize_phase``.

Exercises the post-upsert summarise path end-to-end against a real
``NetworkXGraphStore`` with a stubbed LLM. The store walks, threshold
gating, write-back, and relation re-embed are all real code paths —
only the LLM round-trip is mocked.

Distinct from ``tests/test_summarize.py`` which covers the
``graph.summarize`` module in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from graph.base import Entity, Relation
from graph.networkx_store import NetworkXGraphStore
from ingestion.pipeline import IngestionPipeline

# ---------------------------------------------------------------------------
# Fixtures: a minimal IngestionPipeline + bloated graph state
# ---------------------------------------------------------------------------


def _bare_pipeline(graph_store, embedder=None) -> IngestionPipeline:
    """Construct a pipeline shell that only exposes what
    ``_summarize_phase`` reads. The other ingest dependencies aren't
    touched by the method under test, so we leave them as ``None`` to
    keep the fixture trivial."""
    p = IngestionPipeline.__new__(IngestionPipeline)  # bypass __init__
    p.graph_store = graph_store
    p.embedder = embedder
    return p


def _kg_cfg(**summary_overrides):
    """Mimic the pydantic ``KGExtractionConfig`` shape that
    ``_summarize_phase`` reads — ``getattr``-friendly so a plain
    SimpleNamespace works."""
    summary = SimpleNamespace(
        enabled=True,
        trigger_tokens=1200,
        force_on_count=4,  # low for tests
        max_output_tokens=600,
        context_size=12000,
        max_iterations=5,
        model=None,
        api_key=None,
        api_key_env=None,
        api_base=None,
        timeout=60.0,
        max_workers=2,
        language="Write the entire output in the original language of the input descriptions",
    )
    for k, v in summary_overrides.items():
        setattr(summary, k, v)
    return SimpleNamespace(
        model="openai/gpt-4o-mini",
        api_key=None,
        api_base=None,
        summary=summary,
    )


def _seeded_store(tmp_path) -> NetworkXGraphStore:
    """Spin up a fresh NetworkX store with a couple of entities, one
    of which has a bloated multi-fragment description that should
    cross the count threshold."""
    store = NetworkXGraphStore(path=str(tmp_path / "kg.json"))
    bloated = Entity(
        entity_id="ent_bloated",
        name="Sustainability",
        entity_type="CONCEPT",
        # 5 newline-joined fragments → trips force_on_count=4
        description="\n".join(f"Fragment number {i} about sustainability." for i in range(5)),
        source_doc_ids={"doc_a"},
        source_chunk_ids={"chunk_1"},
    )
    small = Entity(
        entity_id="ent_small",
        name="Bee",
        entity_type="ANIMAL",
        description="A flying insect that pollinates flowers.",
        source_doc_ids={"doc_a"},
        source_chunk_ids={"chunk_1"},
    )
    store.upsert_entity(bloated)
    store.upsert_entity(small)
    # A bloated relation between the two
    rel = Relation(
        relation_id="rel_bloated",
        source_entity="ent_bloated",
        target_entity="ent_small",
        keywords="affects",
        description="\n".join(f"Detail {i} about how sustainability affects bees." for i in range(5)),
        weight=1.0,
        source_doc_ids={"doc_a"},
        source_chunk_ids={"chunk_1"},
    )
    store.upsert_relation(rel)
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_summarize_phase_compacts_bloated_entity(tmp_path):
    """A bloated entity description gets replaced with the LLM summary."""
    store = _seeded_store(tmp_path)
    pipeline = _bare_pipeline(store)
    cfg = _kg_cfg()

    with patch(
        "graph.summarize._call_llm",
        return_value="Sustainability is the canonical merged paragraph.",
    ) as mock_llm:
        pipeline._summarize_phase(["ent_bloated", "ent_small"], [], cfg)

    # Bloated entity got compacted; the small one was below threshold.
    bloated = store.get_entity("ent_bloated")
    small = store.get_entity("ent_small")
    assert bloated.description == "Sustainability is the canonical merged paragraph."
    assert small.description == "A flying insect that pollinates flowers."
    # Exactly one LLM call (only the bloated entity tripped the gate).
    assert mock_llm.call_count == 1


def test_summarize_phase_compacts_relation_and_reembeds(tmp_path):
    """Relations get summarised AND re-embedded so vector search stays fresh."""
    store = _seeded_store(tmp_path)

    embed_calls: list[list[str]] = []

    class StubEmbedder:
        def embed_texts(self, texts):
            embed_calls.append(list(texts))
            return [[0.5, 0.5, 0.5]]  # one vector per text

    pipeline = _bare_pipeline(store, embedder=StubEmbedder())
    cfg = _kg_cfg()

    with patch(
        "graph.summarize._call_llm",
        return_value="Sustainability and bees, merged.",
    ):
        pipeline._summarize_phase([], ["rel_bloated"], cfg)

    # Walk the store to find the relation post-summary.
    rel = pipeline._get_relation_by_id("rel_bloated")
    assert rel is not None
    assert rel.description == "Sustainability and bees, merged."
    assert rel.description_embedding == [0.5, 0.5, 0.5]
    # Embedder was invoked exactly once with the new summary text.
    assert embed_calls == [["Sustainability and bees, merged."]]


def test_summarize_phase_skips_when_disabled(tmp_path):
    """``summary.enabled = False`` is a hard kill switch."""
    store = _seeded_store(tmp_path)
    pipeline = _bare_pipeline(store)
    cfg = _kg_cfg(enabled=False)

    with patch("graph.summarize._call_llm") as mock_llm:
        pipeline._summarize_phase(["ent_bloated"], ["rel_bloated"], cfg)

    assert mock_llm.call_count == 0
    # Description unchanged — still the multi-line concat.
    assert "Fragment number 0" in store.get_entity("ent_bloated").description


def test_summarize_phase_skips_when_below_threshold(tmp_path):
    """Entities/relations below the threshold don't burn LLM calls."""
    store = _seeded_store(tmp_path)
    pipeline = _bare_pipeline(store)
    # Crank thresholds high so nothing trips
    cfg = _kg_cfg(force_on_count=99, trigger_tokens=99999)

    with patch("graph.summarize._call_llm") as mock_llm:
        pipeline._summarize_phase(["ent_bloated", "ent_small"], ["rel_bloated"], cfg)

    assert mock_llm.call_count == 0


def test_summarize_phase_llm_failure_keeps_original(tmp_path):
    """A summarise failure leaves the verbatim description in place."""
    store = _seeded_store(tmp_path)
    pipeline = _bare_pipeline(store)
    cfg = _kg_cfg()

    original = store.get_entity("ent_bloated").description

    with patch("graph.summarize._call_llm", side_effect=RuntimeError("provider down")):
        pipeline._summarize_phase(["ent_bloated"], [], cfg)

    # Description unchanged — graceful degradation.
    assert store.get_entity("ent_bloated").description == original


def test_summarize_phase_dedupes_repeated_ids(tmp_path):
    """If a caller passes the same id twice we summarise once.

    The pipeline integration sends ``[e.entity_id for e in entities]``
    where the same entity could appear twice if it gets re-extracted
    from multiple chunks in the same doc. Re-summarising the same
    entity in the same phase is wasted LLM spend.
    """
    store = _seeded_store(tmp_path)
    pipeline = _bare_pipeline(store)
    cfg = _kg_cfg()

    with patch(
        "graph.summarize._call_llm",
        return_value="merged",
    ) as mock_llm:
        pipeline._summarize_phase(["ent_bloated", "ent_bloated", "ent_bloated"], [], cfg)

    # De-duped to a single LLM call despite the triple-id payload.
    assert mock_llm.call_count == 1
