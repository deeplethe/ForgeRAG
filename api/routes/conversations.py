"""
/api/v1/conversations — multi-turn chat management

    GET    /api/v1/conversations                       list current user's conversations
    POST   /api/v1/conversations                       create empty (auto-owned by caller)
    GET    /api/v1/conversations/{id}                  detail + message count
    DELETE /api/v1/conversations/{id}                  delete (cascade messages)
    PATCH  /api/v1/conversations/{id}                  update title
    GET    /api/v1/conversations/{id}/messages         message history
    POST   /api/v1/conversations/{id}/messages         append a message

Privacy contract:

    Conversations are user-private. Even ``role=admin`` does NOT
    bypass — admin role is for shared-corpus management (folders /
    tokens / users), not for reading other users' chat history.
    Cross-user lookups consistently return 404 (not 403) so the
    endpoint never confirms whether a conversation_id belongs to
    someone else.

When auth is disabled the synthetic ``local`` admin owns
conversations either created with ``user_id=None`` (legacy) or
``user_id="local"`` (post-S4 dev runs). Both surface to the
``local`` principal as their own — see ``_owner_predicate``.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import AuthenticatedPrincipal
from ..deps import get_principal, get_state
from ..schemas import ConversationOut, MessageOut, PaginatedResponse
from ..state import AppState

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


class CreateConversationRequest(BaseModel):
    title: str | None = None


class UpdateConversationRequest(BaseModel):
    """Partial-update payload. Both fields optional — clients
    send just what they want to change. The handler skips
    unchanged fields rather than overwriting with None / False
    (PATCH semantics, not PUT)."""

    title: str | None = None
    is_favorite: bool | None = None


# ---------------------------------------------------------------------------
# Privacy helpers
# ---------------------------------------------------------------------------


def _effective_owner(state: AppState, principal: AuthenticatedPrincipal) -> str | None:
    """Return the ``user_id`` to write on new conversations + filter
    list/get against. ``None`` means "no filter" — used when auth is
    disabled so single-user dev sees every legacy conversation."""
    if not state.cfg.auth.enabled:
        return None
    return principal.user_id


def _owns_conversation(row: dict, owner_user_id: str | None) -> bool:
    """Privacy check applied to every per-conversation route.

    A row is "owned" by the caller iff:
      * filter is None (auth disabled — see above)
      * row.user_id matches caller's user_id
      * row.user_id is NULL and the caller is the synthetic ``local``
        admin (legacy rows pre-date the user_id column).
    """
    if owner_user_id is None:
        return True
    row_user = row.get("user_id")
    if row_user == owner_user_id:
        return True
    return row_user is None and owner_user_id == "local"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=PaginatedResponse)
def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    owner = _effective_owner(state, principal)
    rows = state.store.list_conversations(limit=limit, offset=offset, user_id=owner)
    total = state.store.count_conversations(user_id=owner)
    items = []
    for r in rows:
        r["message_count"] = state.store.count_messages(r["conversation_id"])
        items.append(ConversationOut(**{k: r[k] for k in ConversationOut.model_fields if k in r}))
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=ConversationOut, status_code=201)
def create_conversation(
    req: CreateConversationRequest = CreateConversationRequest(),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    cid = uuid4().hex
    state.store.create_conversation(
        {
            "conversation_id": cid,
            "title": req.title,
            "user_id": _effective_owner(state, principal),
        }
    )
    row = state.store.get_conversation(cid)
    return ConversationOut(**row, message_count=0)


@router.get("/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    row = state.store.get_conversation(conversation_id)
    if not row or not _owns_conversation(row, _effective_owner(state, principal)):
        # 404 on cross-user access — never confirm a stranger's
        # conversation exists. Same code as "doesn't exist."
        raise HTTPException(404, "conversation not found")
    row["message_count"] = state.store.count_messages(conversation_id)
    return ConversationOut(**{k: row[k] for k in ConversationOut.model_fields if k in row})


@router.patch("/{conversation_id}", response_model=ConversationOut)
def update_conversation(
    conversation_id: str,
    req: UpdateConversationRequest,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    row = state.store.get_conversation(conversation_id)
    if not row or not _owns_conversation(row, _effective_owner(state, principal)):
        raise HTTPException(404, "conversation not found")
    # PATCH semantics — only forward fields the client actually
    # set. ``update_conversation`` setattr-only so passing
    # explicit ``None`` would NULL the column.
    updates: dict = {}
    if req.title is not None:
        updates["title"] = req.title
    if req.is_favorite is not None:
        updates["is_favorite"] = req.is_favorite
    if updates:
        state.store.update_conversation(conversation_id, **updates)
    return get_conversation(conversation_id, state, principal)


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    row = state.store.get_conversation(conversation_id)
    if not row or not _owns_conversation(row, _effective_owner(state, principal)):
        raise HTTPException(404, "conversation not found")
    state.store.delete_conversation(conversation_id)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(
    conversation_id: str,
    limit: int = Query(100, ge=1, le=500),
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    row = state.store.get_conversation(conversation_id)
    if not row or not _owns_conversation(row, _effective_owner(state, principal)):
        raise HTTPException(404, "conversation not found")
    msgs = state.store.get_messages(conversation_id, limit=limit)
    return [MessageOut(**{k: m[k] for k in MessageOut.model_fields if k in m}) for m in msgs]


class AddMessageRequest(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str


@router.post("/{conversation_id}/messages", response_model=MessageOut, status_code=201)
def add_message(
    conversation_id: str,
    req: AddMessageRequest,
    state: AppState = Depends(get_state),
    principal: AuthenticatedPrincipal = Depends(get_principal),
):
    """Manually add a message to a conversation (used for preset Q&A)."""
    row = state.store.get_conversation(conversation_id)
    if not row or not _owns_conversation(row, _effective_owner(state, principal)):
        raise HTTPException(404, "conversation not found")
    mid = uuid4().hex
    state.store.add_message(
        {
            "message_id": mid,
            "conversation_id": conversation_id,
            "role": req.role,
            "content": req.content,
        }
    )
    return MessageOut(
        message_id=mid,
        conversation_id=conversation_id,
        role=req.role,
        content=req.content,
    )
