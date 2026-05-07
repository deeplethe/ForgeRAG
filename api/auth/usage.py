"""
Per-user LLM token usage aggregation.

The agent loop's final ``done`` event carries ``tokens_in`` /
``tokens_out`` for the turn. Those are written onto the ASSISTANT
message row (``messages.input_tokens`` / ``output_tokens``) by
``api/routes/agent.py::_persist_assistant_message``. This module is the read
side: SUM over messages → conversations → user_id, with two
shapes:

  * ``user_usage(sess, user_id)``  → totals for one user
  * ``all_user_usage(sess)``        → list with one row per user

Three views consume this:

  * ``GET /auth/me/usage``           — every user, own stats
  * ``GET /admin/users/{id}/usage``  — admin viewing one user
  * ``GET /metrics/usage/by-user``   — admin aggregate (top N)

Edge cases handled:
  * Conversations where ``user_id IS NULL`` (synthesised
    ``"local"`` deploys, or conversations from before
    multi-user landed) are bucketed under the literal
    string ``"local"`` so the aggregate stays usable.
  * Users with zero messages don't appear in
    ``all_user_usage`` — the GROUP BY runs over messages,
    so a user who has never asked anything is implicitly
    zero. Callers that need "every user even if zero" can
    LEFT JOIN against the auth_users table themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from persistence.models import Conversation, Message


@dataclass
class UsageTotals:
    user_id: str | None
    input_tokens: int
    output_tokens: int
    message_count: int

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


def user_usage(sess: Session, user_id: str) -> UsageTotals:
    """Totals across every assistant turn this user has run.

    Uses an aggregate of zero rather than NULL when the user has
    no messages, so callers don't have to None-check before
    rendering "0 tokens".
    """
    row = sess.execute(
        select(
            func.coalesce(func.sum(Message.input_tokens), 0),
            func.coalesce(func.sum(Message.output_tokens), 0),
            func.count(Message.message_id),
        )
        .select_from(Message)
        .join(Conversation, Conversation.conversation_id == Message.conversation_id)
        .where(
            Conversation.user_id == user_id,
            Message.role == "assistant",
        )
    ).one()
    return UsageTotals(
        user_id=user_id,
        input_tokens=int(row[0] or 0),
        output_tokens=int(row[1] or 0),
        message_count=int(row[2] or 0),
    )


def all_user_usage(sess: Session) -> list[UsageTotals]:
    """One row per user that has at least one assistant message.

    Sorted by total tokens DESC so the admin metrics view can
    just slice ``[:N]`` for a top-N display. ``user_id IS NULL``
    rows (legacy / auth-disabled deploys) bucket under
    ``"local"`` — see module docstring.
    """
    user_id_col = func.coalesce(Conversation.user_id, "local").label("user_id")
    rows = sess.execute(
        select(
            user_id_col,
            func.coalesce(func.sum(Message.input_tokens), 0),
            func.coalesce(func.sum(Message.output_tokens), 0),
            func.count(Message.message_id),
        )
        .select_from(Message)
        .join(Conversation, Conversation.conversation_id == Message.conversation_id)
        .where(Message.role == "assistant")
        .group_by(user_id_col)
    ).all()
    out = [
        UsageTotals(
            user_id=str(r[0]) if r[0] is not None else "local",
            input_tokens=int(r[1] or 0),
            output_tokens=int(r[2] or 0),
            message_count=int(r[3] or 0),
        )
        for r in rows
    ]
    out.sort(key=lambda u: u.total_tokens, reverse=True)
    return out
