"""
FastAPI integration tests against /api/v1/* routes.

Uses SQLite + local blob + fake embedder/vector/LLM.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("fitz")

from fastapi.testclient import TestClient

from api.app import create_app
from api.state import AppState
from config import (
    AppConfig,
    FilesConfig,
    LocalStorageModel,
    RelationalConfig,
    SQLiteConfig,
    StorageModel,
)

from .test_ingestion_pipeline import FakeEmbedder, FakeVectorStore


def _wait_ingest(state):
    """Block until all queued ingestion jobs finish."""
    state.ingest_queue._queue.join()


class FakeGenerator:
    backend = "fake"
    model = "fake/test"

    def generate(self, messages, *, overrides=None):
        from answering.prompts import extract_cited_ids

        content = "\n".join(m["content"] for m in messages if m["role"] == "user")
        marker = "[c_1]"
        text = f"Answer {marker}." if marker in content else "I don't know."
        return {
            "text": text,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            "model": self.model,
            "cited_ids": extract_cited_ids(text),
            "latency_ms": 2,
        }


@pytest.fixture
def client(tmp_path, sample_pdf):
    cfg = AppConfig()
    cfg.persistence.relational = RelationalConfig(
        backend="sqlite",
        sqlite=SQLiteConfig(path=str(tmp_path / "hi.db")),
    )
    cfg.storage = StorageModel(
        mode="local",
        local=LocalStorageModel(root=str(tmp_path / "blobs"), public_base_url=None),
    )
    cfg.files = FilesConfig()
    cfg.embedder.dimension = 4
    cfg.persistence.vector.pgvector.dimension = 4

    fake_vec = FakeVectorStore()
    fake_emb = FakeEmbedder()
    state = AppState(cfg, vector_store=fake_vec, embedder=fake_emb)

    from answering.pipeline import AnsweringPipeline

    state._answering = AnsweringPipeline(
        cfg.answering,
        retrieval=state.retrieval,
        generator=FakeGenerator(),
        store=state.store,
    )

    app = create_app(state=state)
    with TestClient(app) as c:
        yield c, state, sample_pdf
    state.shutdown()


# --- Health ---


class TestHealth:
    def test_ok(self, client):
        c, _, _ = client
        r = c.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert "counts" in r.json()


# --- Files ---


class TestFiles:
    def test_upload(self, client, sample_pdf):
        c, *_ = client
        with open(sample_pdf, "rb") as f:
            r = c.post("/api/v1/files", files={"file": ("s.pdf", f, "application/pdf")})
        assert r.status_code == 201
        assert r.json()["file_id"]

    def test_list(self, client, sample_pdf):
        c, *_ = client
        with open(sample_pdf, "rb") as f:
            c.post("/api/v1/files", files={"file": ("s.pdf", f, "application/pdf")})
        r = c.get("/api/v1/files")
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    def test_get(self, client, sample_pdf):
        c, *_ = client
        with open(sample_pdf, "rb") as f:
            up = c.post("/api/v1/files", files={"file": ("s.pdf", f, "application/pdf")}).json()
        r = c.get(f"/api/v1/files/{up['file_id']}")
        assert r.status_code == 200

    def test_download(self, client, sample_pdf):
        c, *_ = client
        with open(sample_pdf, "rb") as f:
            up = c.post("/api/v1/files", files={"file": ("s.pdf", f, "application/pdf")}).json()
        r = c.get(f"/api/v1/files/{up['file_id']}/download")
        assert r.status_code == 200
        assert len(r.content) == up["size_bytes"]

    def test_delete(self, client, sample_pdf):
        c, *_ = client
        with open(sample_pdf, "rb") as f:
            up = c.post("/api/v1/files", files={"file": ("s.pdf", f, "application/pdf")}).json()
        r = c.delete(f"/api/v1/files/{up['file_id']}")
        assert r.status_code == 204

    def test_not_found(self, client):
        c, *_ = client
        assert c.get("/api/v1/files/nope").status_code == 404

    def test_bad_mime(self, client):
        c, *_ = client
        r = c.post("/api/v1/files", files={"file": ("x.exe", b"MZ", "application/x-msdownload")})
        assert r.status_code == 400


# --- Documents ---


class TestDocuments:
    def _upload(self, c, sample_pdf):
        with open(sample_pdf, "rb") as f:
            return c.post("/api/v1/files", files={"file": ("s.pdf", f, "application/pdf")}).json()

    def test_ingest(self, client, sample_pdf):
        c, state, _ = client
        up = self._upload(c, sample_pdf)
        r = c.post("/api/v1/documents", json={"file_id": up["file_id"]})
        assert r.status_code == 202, r.text
        assert r.json()["doc_id"]
        _wait_ingest(state)
        doc = c.get(f"/api/v1/documents/{r.json()['doc_id']}").json()
        assert doc["num_chunks"] > 0

    def test_upload_and_ingest(self, client, sample_pdf):
        c, state, _ = client
        with open(sample_pdf, "rb") as f:
            r = c.post(
                "/api/v1/documents/upload-and-ingest",
                files={"file": ("s.pdf", f, "application/pdf")},
            )
        assert r.status_code == 202, r.text
        assert r.json()["doc_id"]
        _wait_ingest(state)

    def test_list(self, client, sample_pdf):
        c, state, _ = client
        with open(sample_pdf, "rb") as f:
            c.post("/api/v1/documents/upload-and-ingest", files={"file": ("s.pdf", f, "application/pdf")})
        _wait_ingest(state)
        r = c.get("/api/v1/documents")
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    def test_get_detail(self, client, sample_pdf):
        c, state, _ = client
        with open(sample_pdf, "rb") as f:
            ing = c.post("/api/v1/documents/upload-and-ingest", files={"file": ("s.pdf", f, "application/pdf")}).json()
        _wait_ingest(state)
        r = c.get(f"/api/v1/documents/{ing['doc_id']}")
        assert r.status_code == 200

    def test_delete(self, client, sample_pdf):
        c, state, _ = client
        with open(sample_pdf, "rb") as f:
            ing = c.post("/api/v1/documents/upload-and-ingest", files={"file": ("s.pdf", f, "application/pdf")}).json()
        _wait_ingest(state)
        r = c.delete(f"/api/v1/documents/{ing['doc_id']}")
        assert r.status_code == 204

    def test_blocks(self, client, sample_pdf):
        c, state, _ = client
        with open(sample_pdf, "rb") as f:
            ing = c.post("/api/v1/documents/upload-and-ingest", files={"file": ("s.pdf", f, "application/pdf")}).json()
        _wait_ingest(state)
        r = c.get(f"/api/v1/documents/{ing['doc_id']}/blocks")
        assert r.status_code == 200
        assert r.json()["total"] > 0

    def test_chunks(self, client, sample_pdf):
        c, state, _ = client
        with open(sample_pdf, "rb") as f:
            ing = c.post("/api/v1/documents/upload-and-ingest", files={"file": ("s.pdf", f, "application/pdf")}).json()
        _wait_ingest(state)
        r = c.get(f"/api/v1/documents/{ing['doc_id']}/chunks")
        assert r.status_code == 200
        assert r.json()["total"] > 0

    def test_tree(self, client, sample_pdf):
        c, state, _ = client
        with open(sample_pdf, "rb") as f:
            ing = c.post("/api/v1/documents/upload-and-ingest", files={"file": ("s.pdf", f, "application/pdf")}).json()
        _wait_ingest(state)
        r = c.get(f"/api/v1/documents/{ing['doc_id']}/tree")
        assert r.status_code == 200
        assert r.json()["root_id"]


# --- Chunks / Blocks standalone ---


class TestChunksBlocks:
    def _ingest(self, c, state, sample_pdf):
        with open(sample_pdf, "rb") as f:
            resp = c.post("/api/v1/documents/upload-and-ingest", files={"file": ("s.pdf", f, "application/pdf")}).json()
        _wait_ingest(state)
        return resp

    def test_get_chunk(self, client, sample_pdf):
        c, state, _ = client
        ing = self._ingest(c, state, sample_pdf)
        chunks = c.get(f"/api/v1/documents/{ing['doc_id']}/chunks").json()
        cid = chunks["items"][0]["chunk_id"]
        r = c.get(f"/api/v1/chunks/{cid}")
        assert r.status_code == 200
        assert r.json()["content"]

    def test_get_block(self, client, sample_pdf):
        c, state, _ = client
        ing = self._ingest(c, state, sample_pdf)
        blocks = c.get(f"/api/v1/documents/{ing['doc_id']}/blocks").json()
        bid = blocks["items"][0]["block_id"]
        r = c.get(f"/api/v1/blocks/{bid}")
        assert r.status_code == 200
        assert "bbox" in r.json()


# --- Query ---


class TestQuery:
    def test_ask(self, client, sample_pdf):
        c, state, _ = client
        with open(sample_pdf, "rb") as f:
            c.post("/api/v1/documents/upload-and-ingest", files={"file": ("s.pdf", f, "application/pdf")})
        _wait_ingest(state)
        # Test fixtures have no LLM credentials; QU and rerank now run on
        # every retrieve unless explicitly disabled, so opt out per-request.
        r = c.post(
            "/api/v1/query",
            json={
                "query": "introduction",
                "overrides": {"query_understanding": False, "rerank": False},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["finish_reason"] in ("stop", "no_context")

    def test_empty_query(self, client):
        c, *_ = client
        r = c.post("/api/v1/query", json={"query": "   "})
        assert r.status_code == 400


# --- Settings ---


class TestSettings:
    def test_list_all(self, client):
        c, *_ = client
        r = c.get("/api/v1/settings")
        assert r.status_code == 200
        assert "groups" in r.json()

    def test_settings_are_read_only(self, client):
        # All mutating settings routes were removed: yaml is the single
        # source of truth, edit the file and restart to change config.
        c, *_ = client
        r = c.put(
            "/api/v1/settings/key/retrieval.vector.top_k",
            json={"value_json": 99},
        )
        assert r.status_code == 405
        r = c.delete("/api/v1/settings/key/retrieval.vector.top_k")
        assert r.status_code == 405


# --- Traces ---


class TestTraces:
    def test_list(self, client):
        c, *_ = client
        r = c.get("/api/v1/traces")
        assert r.status_code == 200
