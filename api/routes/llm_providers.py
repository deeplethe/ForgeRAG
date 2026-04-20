"""
LLM Providers — pluggable model registry.

CRUD for managing chat / embedding / reranker endpoints.
The API never exposes raw API keys in GET responses (only a boolean flag).
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_state
from ..schemas import LLMProviderCreate, LLMProviderOut, LLMProviderUpdate
from ..state import AppState

router = APIRouter(prefix="/api/v1/llm-providers", tags=["llm-providers"])


# ---------------------------------------------------------------------------
# Presets (read-only catalogue)
# ---------------------------------------------------------------------------


@router.get("/presets")
def list_presets(provider_type: str | None = None) -> dict:
    """Return curated provider presets, optionally filtered by type."""
    from config.provider_presets import PROVIDER_PRESETS, presets_for_type

    data = presets_for_type(provider_type) if provider_type else PROVIDER_PRESETS
    return {"presets": data}


# ---------------------------------------------------------------------------
# Test connection
# ---------------------------------------------------------------------------


@router.post("/{provider_id}/test")
def test_provider(provider_id: str, state: AppState = Depends(get_state)) -> dict:
    """
    Send a minimal probe request to the provider's endpoint and return
    the latency + outcome. Used by the "Test Connection" button in the
    Provider edit modal to catch schema/key/endpoint mismatches before
    the user runs a real query.

    Different provider_types use different probes:
        chat / vlm   — litellm.completion({role: user, content: "ping"})
        embedding    — litellm.embedding(input=["ping"])
        reranker     — litellm.rerank(query="ping", documents=[...])
    """
    row = state.store.get_llm_provider(provider_id)
    if not row:
        raise HTTPException(404, f"provider {provider_id!r} not found")

    import litellm

    kwargs: dict = {}
    if row.get("api_key"):
        kwargs["api_key"] = row["api_key"]
    if row.get("api_base"):
        kwargs["api_base"] = row["api_base"]

    t0 = time.time()
    try:
        ptype = row["provider_type"]
        if ptype == "reranker":
            resp = litellm.rerank(
                model=row["model_name"],
                query="ping",
                documents=["the quick brown fox", "hello world"],
                top_n=2,
                timeout=15.0,
                **kwargs,
            )
            results = getattr(resp, "results", None)
            if not results and isinstance(resp, dict):
                results = resp.get("results")
            ok = bool(results)
            preview = f"got {len(results) if results else 0} ranked results"
        elif ptype == "embedding":
            resp = litellm.embedding(
                model=row["model_name"],
                input=["ping"],
                timeout=15.0,
                **kwargs,
            )
            data = getattr(resp, "data", None)
            if not data and isinstance(resp, dict):
                data = resp.get("data")
            ok = bool(data)
            preview = f"got embedding dim={len(data[0]['embedding']) if data else 0}"
        else:  # chat, vlm
            resp = litellm.completion(
                model=row["model_name"],
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8,
                timeout=15.0,
                **kwargs,
            )
            content = ""
            try:
                content = resp.choices[0].message.content or ""
            except Exception:
                pass
            ok = True
            preview = f"response: {content[:60]!r}"
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        inner = f" (cause: {type(cause).__name__}: {cause})" if cause is not None else ""
        suggested = _suggest_fix(type(e).__name__, str(e), row)
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error_type": type(e).__name__,
            "message": f"{e}{inner}",
            "suggested_fix": suggested,
        }

    latency_ms = int((time.time() - t0) * 1000)
    return {
        "ok": ok,
        "latency_ms": latency_ms,
        "response_preview": preview,
    }


def _suggest_fix(err_type: str, err_msg: str, provider: dict) -> str | None:
    """Return a short hint string for common configuration mistakes."""
    msg_l = err_msg.lower()
    if "llm provider not provided" in msg_l or "provider list" in msg_l:
        return (
            "Model string is missing a recognized LiteLLM provider prefix. "
            "For SiliconFlow rerank, prefix with 'jina_ai/' (not 'siliconflow/')."
        )
    if "401" in err_msg or "unauthorized" in msg_l or "invalid" in msg_l and "api" in msg_l:
        return "401 Unauthorized — check the API key."
    if "not found" in msg_l and "model" in msg_l:
        return f"Model {provider.get('model_name')!r} not found at this endpoint — check model name/spelling."
    if err_type == "APIConnectionError":
        return "Connection failed — check api_base URL and network reachability."
    if "schema" in msg_l or "texts" in msg_l or "documents" in msg_l:
        return (
            "Request schema mismatch. For Cohere-compat rerank endpoints (Jina/SiliconFlow), "
            "use 'jina_ai/<model>'. For TEI servers, use 'huggingface/<model>'."
        )
    return None


def _to_out(row: dict) -> LLMProviderOut:
    return LLMProviderOut(
        id=row["id"],
        name=row["name"],
        provider_type=row["provider_type"],
        api_base=row["api_base"],
        model_name=row["model_name"],
        api_key_set=bool(row.get("api_key")),
        is_default=row.get("is_default", False),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


@router.get("", response_model=list[LLMProviderOut])
def list_providers(
    provider_type: str | None = None,
    state: AppState = Depends(get_state),
):
    """List all registered LLM providers, optionally filtered by type."""
    rows = state.store.list_llm_providers(provider_type=provider_type)
    return [_to_out(r) for r in rows]


@router.get("/{provider_id}", response_model=LLMProviderOut)
def get_provider(provider_id: str, state: AppState = Depends(get_state)):
    row = state.store.get_llm_provider(provider_id)
    if not row:
        raise HTTPException(404, f"provider {provider_id!r} not found")
    return _to_out(row)


@router.post("", response_model=LLMProviderOut, status_code=201)
def create_provider(body: LLMProviderCreate, state: AppState = Depends(get_state)):
    if body.provider_type not in ("chat", "embedding", "reranker", "vlm"):
        raise HTTPException(422, "provider_type must be chat, embedding, reranker, or vlm")
    existing = state.store.get_llm_provider_by_name(body.name)
    if existing:
        raise HTTPException(409, f"provider name {body.name!r} already exists")
    record = {
        "id": uuid.uuid4().hex[:16],
        "name": body.name,
        "provider_type": body.provider_type,
        "api_base": body.api_base,
        "model_name": body.model_name,
        "api_key": body.api_key,
        "is_default": body.is_default,
    }
    state.store.upsert_llm_provider(record)
    row = state.store.get_llm_provider(record["id"])
    return _to_out(row)


@router.put("/{provider_id}", response_model=LLMProviderOut)
def update_provider(
    provider_id: str,
    body: LLMProviderUpdate,
    state: AppState = Depends(get_state),
):
    existing = state.store.get_llm_provider(provider_id)
    if not existing:
        raise HTTPException(404, f"provider {provider_id!r} not found")
    if body.provider_type and body.provider_type not in ("chat", "embedding", "reranker", "vlm"):
        raise HTTPException(422, "provider_type must be chat, embedding, reranker, or vlm")
    updates = body.model_dump(exclude_none=True)
    if updates:
        updates["id"] = provider_id
        state.store.upsert_llm_provider(updates)

    # Re-resolve all provider_id references so live config picks up changes
    from config.settings_manager import resolve_providers

    resolve_providers(state.cfg, state.store)
    state._retrieval = None
    state._answering = None

    row = state.store.get_llm_provider(provider_id)
    return _to_out(row)


@router.delete("/{provider_id}")
def delete_provider(provider_id: str, state: AppState = Depends(get_state)):
    existing = state.store.get_llm_provider(provider_id)
    if not existing:
        raise HTTPException(404, f"provider {provider_id!r} not found")
    state.store.delete_llm_provider(provider_id)

    # Re-resolve (the deleted provider's fields will no longer match)
    from config.settings_manager import resolve_providers

    resolve_providers(state.cfg, state.store)
    state._retrieval = None
    state._answering = None

    return {"deleted": provider_id}
