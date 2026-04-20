"""Cross-lingual entity resolution tests.

Reproduces the "bee / 蜜蜂" bug: the KG has English entity names, the
user queries in Chinese, embedder is multilingual (so English and
Chinese words land near each other in vector space). The KG retrieval
path must bridge the language gap via name-embedding cosine search,
falling back to fuzzy name match only when that fails.

These tests use a fake in-memory graph store (or NetworkXGraphStore
on a temp file) plus a deterministic fake embedder that simulates
multilingual behavior — Chinese and English equivalents share a
vector, unrelated terms get orthogonal vectors.
"""

from __future__ import annotations

from pathlib import Path

from graph.base import Entity, Relation, entity_id_from_name
from graph.networkx_store import NetworkXGraphStore
from retrieval.kg_path import KGPath

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeMultilingualEmbedder:
    """Deterministic 'multilingual' embedder for tests.

    Words listed in the same equivalence class share an identical
    vector, so cosine similarity between them is 1.0. Unknown words
    get a unique orthogonal vector each, so they're independent.
    """

    EQUIV_CLASSES = [
        ("bee", "bees", "Bee", "蜜蜂"),
        ("beekeeper", "beekeepers", "养蜂人"),
        ("honey", "蜂蜜"),
        ("car", "汽车"),
    ]

    def __init__(self, dim: int = 16):
        self.dim = dim
        # Assign basis vectors to each equivalence class.
        self._word_to_class: dict[str, int] = {}
        for i, cls in enumerate(self.EQUIV_CLASSES):
            for w in cls:
                self._word_to_class[w] = i
        self._unknown_counter = len(self.EQUIV_CLASSES)

    def _vec_for_class(self, class_idx: int) -> list[float]:
        v = [0.0] * self.dim
        v[class_idx % self.dim] = 1.0
        return v

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            if t in self._word_to_class:
                out.append(self._vec_for_class(self._word_to_class[t]))
            else:
                # Unknown word → unique vector in a fresh dimension.
                idx = self._unknown_counter
                self._word_to_class[t] = idx
                self._unknown_counter += 1
                out.append(self._vec_for_class(idx))
        return out


class FakeStore:
    """Minimal relational store: get_chunks_by_ids returns back whatever ids were asked for."""

    def __init__(self, known_chunk_ids: set[str]):
        self._known = known_chunk_ids

    def get_chunks_by_ids(self, chunk_ids):
        return [{"chunk_id": c, "content": f"content of {c}"} for c in chunk_ids if c in self._known]


class FakeCfg:
    provider_id: str | None = None
    model = "fake"
    api_key = ""
    api_key_env = ""
    api_base = ""
    max_hops = 1
    local_weight = 1.0
    global_weight = 1.0
    relation_weight = 0.0
    relation_top_k = 10
    top_k = 30


class FakeExtractor:
    """Always returns a fixed (entity_names, keywords) pair."""

    def __init__(self, names, keywords):
        self._names = names
        self._keywords = keywords

    def extract_query_entities(self, query: str):
        return (self._names, self._keywords)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_graph(tmp_path: Path, embedder: FakeMultilingualEmbedder) -> NetworkXGraphStore:
    graph = NetworkXGraphStore(path=str(tmp_path / "kg.json"))
    # English-named entities, with name_embedding computed by the (fake multilingual) embedder.
    bee = Entity(
        name="bee",
        description="a small flying insect that produces honey",
        source_chunk_ids={"c_bee_1", "c_bee_2"},
    )
    bee.name_embedding = embedder.embed_texts(["bee"])[0]
    beekeeper = Entity(
        name="beekeeper",
        description="a person who keeps bees for honey and pollination",
        source_chunk_ids={"c_bk_1"},
    )
    beekeeper.name_embedding = embedder.embed_texts(["beekeeper"])[0]
    car = Entity(
        name="car",
        description="a motor vehicle with four wheels",
        source_chunk_ids={"c_car_1"},
    )
    car.name_embedding = embedder.embed_texts(["car"])[0]
    for e in (bee, beekeeper, car):
        graph.upsert_entity(e)

    rel = Relation(
        source_entity=bee.entity_id,
        target_entity=beekeeper.entity_id,
        description="beekeepers keep bees",
        source_chunk_ids={"c_rel_1"},
    )
    graph.upsert_relation(rel)
    return graph


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSearchEntitiesByEmbedding:
    def test_networkx_finds_english_entity_from_chinese_query_vec(self, tmp_path: Path):
        emb = FakeMultilingualEmbedder()
        g = _make_graph(tmp_path, emb)
        # Chinese query vector (shared with English 'bee')
        q = emb.embed_texts(["蜜蜂"])[0]
        hits = g.search_entities_by_embedding(q, top_k=3)
        assert hits, "should find at least one match"
        top_entity, score = hits[0]
        assert top_entity.name == "bee"
        assert score > 0.9

    def test_orthogonal_query_scores_low(self, tmp_path: Path):
        emb = FakeMultilingualEmbedder()
        g = _make_graph(tmp_path, emb)
        # Unknown word → orthogonal vector, all entities should score ~0.
        q = emb.embed_texts(["quantumfoobar"])[0]
        hits = g.search_entities_by_embedding(q, top_k=3)
        for _, score in hits:
            assert score < 0.5

    def test_empty_vec_returns_empty(self, tmp_path: Path):
        emb = FakeMultilingualEmbedder()
        g = _make_graph(tmp_path, emb)
        assert g.search_entities_by_embedding([], top_k=5) == []


class TestKGPathCrossLingual:
    def _make_kp(self, tmp_path: Path, entity_names, keywords):
        emb = FakeMultilingualEmbedder()
        g = _make_graph(tmp_path, emb)
        known_chunks = {"c_bee_1", "c_bee_2", "c_bk_1", "c_rel_1", "c_car_1"}
        rel = FakeStore(known_chunks)
        kp = KGPath(
            cfg=FakeCfg(),
            graph=g,
            relational=rel,
            extractor=FakeExtractor(entity_names, keywords),
            embedder=emb,
        )
        return kp

    def test_chinese_query_finds_english_entity(self, tmp_path: Path):
        """End-to-end: query extracts '蜜蜂', graph has 'bee' → hits non-empty."""
        kp = self._make_kp(tmp_path, entity_names=["蜜蜂"], keywords=["蜜蜂"])
        result = kp.search("蜜蜂是什么")
        chunk_ids = {s.chunk_id for s in result}
        assert "c_bee_1" in chunk_ids
        assert "c_bee_2" in chunk_ids
        # Entities in kg_context should surface the English name.
        names = [e["name"] for e in kp.kg_context.entities]
        assert "bee" in names

    def test_chinese_relationship_query(self, tmp_path: Path):
        """'养蜂人与蜜蜂的关系' → both entities + their relation."""
        kp = self._make_kp(
            tmp_path,
            entity_names=["养蜂人", "蜜蜂"],
            keywords=["养蜂人", "蜜蜂"],
        )
        result = kp.search("养蜂人与蜜蜂的关系")
        chunk_ids = {s.chunk_id for s in result}
        # All three entity chunks should surface (hop 0 + neighbor).
        assert "c_bee_1" in chunk_ids
        assert "c_bk_1" in chunk_ids
        # Relation chunk should also surface.
        assert "c_rel_1" in chunk_ids
        # kg_context should carry both entities.
        names = {e["name"] for e in kp.kg_context.entities}
        assert {"bee", "beekeeper"} <= names

    def test_english_query_still_works_unchanged(self, tmp_path: Path):
        """Regression: English query with English entity must still hit via SHA256 path,
        not degrade through the embedding branch."""
        kp = self._make_kp(tmp_path, entity_names=["bee"], keywords=["bee"])
        result = kp.search("what is a bee")
        chunk_ids = {s.chunk_id for s in result}
        assert "c_bee_1" in chunk_ids

    def test_unknown_term_does_not_match_random_entity(self, tmp_path: Path):
        """Safety: a totally unrelated query should not incorrectly pull in entities
        via low-confidence embedding matches (threshold guard)."""
        kp = self._make_kp(
            tmp_path,
            entity_names=["quantumfoobar"],
            keywords=["quantumfoobar"],
        )
        result = kp.search("totally unrelated")
        # No English entity should match (orthogonal vector → cosine ≈ 0).
        assert result == []


class TestEntityIndexSyncOnUpsert:
    """New entities upserted after load must also be findable by embedding."""

    def test_incremental_upsert_appears_in_search(self, tmp_path: Path):
        emb = FakeMultilingualEmbedder()
        g = NetworkXGraphStore(path=str(tmp_path / "kg.json"))

        honey = Entity(name="honey", description="sweet food made by bees", source_chunk_ids={"c_h"})
        honey.name_embedding = emb.embed_texts(["honey"])[0]
        g.upsert_entity(honey)

        # Immediate search (no reload) must find it.
        q = emb.embed_texts(["蜂蜜"])[0]
        hits = g.search_entities_by_embedding(q, top_k=3)
        assert hits
        assert hits[0][0].name == "honey"


class TestDeleteSyncsEntityIndex:
    """After delete_by_doc removes an entity, it must not appear in
    search_entities_by_embedding — otherwise top-k gets polluted by
    silently-dropped ghosts.
    """

    def test_delete_removes_entity_from_embedding_index(self, tmp_path: Path):
        emb = FakeMultilingualEmbedder()
        g = NetworkXGraphStore(path=str(tmp_path / "kg.json"))

        # Two entities; "bee" sourced only from doc1, "car" from doc2.
        bee = Entity(
            name="bee",
            description="flying insect",
            source_doc_ids={"doc1"},
            source_chunk_ids={"doc1:c1"},
        )
        bee.name_embedding = emb.embed_texts(["bee"])[0]
        car = Entity(
            name="car",
            description="motor vehicle",
            source_doc_ids={"doc2"},
            source_chunk_ids={"doc2:c1"},
        )
        car.name_embedding = emb.embed_texts(["car"])[0]
        g.upsert_entity(bee)
        g.upsert_entity(car)

        # Before delete: "蜜蜂" finds "bee"
        q_zh_bee = emb.embed_texts(["蜜蜂"])[0]
        before = g.search_entities_by_embedding(q_zh_bee, top_k=3)
        assert any(e.name == "bee" for e, _ in before)

        # Delete doc1 — "bee" should be fully removed (no other doc refs it).
        removed = g.delete_by_doc("doc1")
        assert removed >= 1

        # After delete: "蜜蜂" must not return "bee" — it's gone from the graph.
        after = g.search_entities_by_embedding(q_zh_bee, top_k=3)
        assert not any(e.name == "bee" for e, _ in after), (
            "deleted entity still surfacing via embedding search — FAISS index not cleaned"
        )
        # And direct lookup also confirms it's gone.
        assert g.get_entity(bee.entity_id) is None

    def test_delete_keeps_shared_entity(self, tmp_path: Path):
        """Deleting one of multiple sources must NOT remove the entity nor
        invalidate its embedding entry — it stays searchable.
        """
        emb = FakeMultilingualEmbedder()
        g = NetworkXGraphStore(path=str(tmp_path / "kg.json"))

        # "bee" sourced from doc1 AND doc2
        bee = Entity(
            name="bee",
            description="flying insect",
            source_doc_ids={"doc1", "doc2"},
            source_chunk_ids={"doc1:c1", "doc2:c1"},
        )
        bee.name_embedding = emb.embed_texts(["bee"])[0]
        g.upsert_entity(bee)

        # Delete doc1: bee.source_doc_ids becomes {doc2} — entity stays.
        g.delete_by_doc("doc1")

        # Embedding search still finds it.
        q_zh_bee = emb.embed_texts(["蜜蜂"])[0]
        hits = g.search_entities_by_embedding(q_zh_bee, top_k=3)
        assert any(e.name == "bee" for e, _ in hits)


class TestBackwardCompat:
    """Entities without name_embedding should still be reachable by SHA256 name lookup."""

    def test_no_embedding_same_language_still_works(self, tmp_path: Path):
        g = NetworkXGraphStore(path=str(tmp_path / "kg.json"))
        # No name_embedding on this entity.
        g.upsert_entity(Entity(name="bee", description="flying insect", source_chunk_ids={"c1"}))

        # Direct SHA256 lookup works.
        eid = entity_id_from_name("bee")
        assert g.get_entity(eid) is not None

        # Embedding search with no embeddings → empty list (not an error).
        assert g.search_entities_by_embedding([0.1] * 16, top_k=3) == []
