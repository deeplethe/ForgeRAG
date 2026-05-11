"""
AgentTaskHandle — in-memory companion to ``agent_runs`` + ``agent_events``.

Owns the event-stream pub/sub for one agent run. Decouples agent execution
from any specific SSE connection: the agent emits events through this
handle, subscribers (current SSE clients) consume from it. When a client
disconnects the agent doesn't notice; when a new client reconnects with
``since=N`` it replays events with seq>N then tails live ones.

Hot path (per event):

    agent.emit(type, payload)          (~µs, non-blocking)
      → seq assigned (monotonic)
      → buffer.append(...)             (deque, soft-trim at buffer_size)
      → for q in subscribers: q.put_nowait(event)  (drop on full)
      → db_queue.put(event)            (background writer drains)
      → if force_flush: await persist before return

Background DB writer batches every ~100ms / 50 events to ``agent_events``,
then bumps ``agent_runs.last_event_seq`` to the max seq in the batch.

Force-flush is the small but critical detail: emit("approval_request"),
emit("ask_human"), emit("done"), emit("interrupted") all set
``force_flush=True``. The handle awaits DB persistence before returning,
so on backend restart we never lose the event that says "the agent is
waiting for X". Other events (token / thought / tool_start / ...) batch
normally — losing a few high-frequency status events is acceptable in
exchange for not blocking the agent loop on every emit.

Sandbox-agnostic: this layer doesn't know about Docker, Firecracker,
or any specific sandbox. The runtime (Inc 3) plugs into ``handle.emit``
regardless of where the agent actually executes.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


# Events that MUST be persisted before emit() returns. Losing any of these
# in a crash would strand the run: a client reconnecting wouldn't know
# the agent is paused waiting for input, or that it ended.
_CRITICAL_EVENT_TYPES = frozenset(
    {
        "approval_request",
        "ask_human",
        "sub_agent_start",
        "sub_agent_done",
        "interrupted",
        "error",
        "done",
    }
)

# How long to wait when joining a subscriber's queue that's near full
# before dropping the event for that subscriber. Other subscribers are
# unaffected; the slow consumer's stream just has gaps (which it can
# detect via seq jumps and reconnect to backfill via DB).
_SUBSCRIBER_PUT_TIMEOUT_S = 0.05

# Idle-keepalive interval used inside ``AgentTaskHandle.subscribe``.
# When the subscriber queue has no event for this many seconds, the
# generator emits a synthetic ``_keepalive`` sentinel so the SSE route
# can flush a ``: ping`` comment without an intermediate proxy / SSH
# tunnel tearing the long-lived connection. Tuned at 15s: short enough
# to beat typical 30s NAT/proxy idle timers, long enough to be cheap.
_KEEPALIVE_S = 15.0


# ---------------------------------------------------------------------------
# Feedback envelopes (the user_inbox payloads — Inc 4 implements consumers)
# ---------------------------------------------------------------------------


@dataclass
class FeedbackEnvelope:
    """One user→agent message. Pushed into ``handle.user_inbox`` by the
    /feedback route. Agent loop drains it at checkpoints.

    Four types:
      - 'interrupt'   stop the run cleanly at next checkpoint
      - 'approve'     unblock a PreToolUse approval (with approval_id)
      - 'deny'        same but reject (carries optional explanation)
      - 'answer'      unblock an ask_human call (with question_id)
      - 'message'     redirect mid-run (inserted as new user msg)
    """

    type: str
    approval_id: str | None = None
    question_id: str | None = None
    message: str | None = None
    modified_input: dict | None = None
    received_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Handle
# ---------------------------------------------------------------------------


@dataclass
class AgentTaskHandle:
    """In-memory companion to one ``agent_runs`` row.

    Construct it, ``await start()`` to spawn the background DB writer,
    pass it into the agent runtime so the runtime can call ``emit(...)``,
    expose it through ``state.active_runs[run_id]`` so SSE subscribers
    can find it. When the run reaches a terminal state call
    ``await close()`` to flush + cancel the writer.

    Threading model: all attribute reads/writes happen on the asyncio
    event loop. The handle is NOT thread-safe; if a synchronous worker
    needs to emit, it must dispatch via ``loop.call_soon_threadsafe``.
    """

    run_id: str
    conversation_id: str
    user_id: str | None
    store: Any  # persistence.store.Store — typed as Any to avoid circular

    # Sub-agent tree (Inc 5 actually populates this)
    parent_run_id: str | None = None
    depth: int = 0
    children: list[str] = field(default_factory=list)

    # Lifecycle
    status: str = "running"  # running / approval_wait / ask_human_wait / done / failed / interrupted
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Event stream (monotonic per run)
    event_seq: int = 0
    buffer_size: int = 2000  # soft cap for in-memory ring; falls through to DB
    event_buffer: deque = field(default_factory=deque)
    subscribers: set[asyncio.Queue] = field(default_factory=set)

    # Background DB writer
    batch_size: int = 50
    batch_interval_s: float = 0.1
    _db_queue: asyncio.Queue | None = None
    _writer_task: asyncio.Task | None = None
    # SQLite is single-writer; even when run_in_executor uses a thread
    # pool, concurrent batched-insert + update statements on the SAME
    # SQLite connection collide ("cannot commit transaction - SQL
    # statements in progress"). Serialize every store write made by
    # this handle through the lock. Postgres deployments don't strictly
    # need this but it's cheap (a few µs of contention) and keeps the
    # SQLite dev path reliable.
    _db_write_lock: asyncio.Lock | None = None
    # Cooperative shutdown flag. close() flips this true and waits for
    # the writer to drain the queue + exit on its own — instead of
    # cancelling mid-flush, which would lose any rows the writer had
    # pulled into a local batch and not yet committed.
    _shutting_down: bool = False

    # HITL channels (consumers wired in Inc 4)
    user_inbox: asyncio.Queue | None = None
    pending_approvals: dict[str, asyncio.Future] = field(default_factory=dict)
    pending_questions: dict[str, asyncio.Future] = field(default_factory=dict)
    # Early-arrival buffers. If /feedback (answer/approval) lands BEFORE
    # the agent's await side has registered its Future — which happens
    # when an external auto-responder reacts to the SSE event faster
    # than ``emit()`` returns control to the calling coroutine — we
    # stash the envelope here keyed by id. wait_for_* drains the buffer
    # at registration time. Without this, ``submit_feedback`` would
    # silently drop the early envelope and the agent would block until
    # tool timeout. Surfaced in round-5 Task N (4 sequential ask_human
    # calls, every single one losing the answer to the race).
    early_answers: dict[str, "FeedbackEnvelope"] = field(default_factory=dict)
    early_approvals: dict[str, "FeedbackEnvelope"] = field(default_factory=dict)

    # Token accounting (display in M units on the wire)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    token_budget_total: int | None = None

    # SSE-drop telemetry (Inc 7 Bug 3 fix). Counter of high-frequency
    # events (token/thought) dropped when a subscriber's queue was full.
    # Logged at warning level every 100; surfaced via /stream debug
    # endpoint later if needed.
    _dropped_token_events: int = 0

    # The asyncio.Task running the agent itself (set by runtime in Inc 3)
    agent_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the background DB writer. Idempotent."""
        if self._writer_task is not None:
            return
        self._db_queue = asyncio.Queue()
        self.user_inbox = asyncio.Queue()
        self._db_write_lock = asyncio.Lock()
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name=f"agent-writer-{self.run_id}"
        )

    async def close(self, *, final_status: str | None = None) -> None:
        """Mark the run terminal: signal the writer to drain on its own,
        wait for it to finish, then persist the final agent_run state.
        Safe to call multiple times.

        ``final_status`` patches ``status`` if provided — runtime sets
        this to "done" / "failed" / "interrupted" before close. None =
        no change.

        Cooperative shutdown protocol: flips ``_shutting_down=True``,
        waits up to 5 s for the writer to drain remaining queued events
        and exit naturally. Falls back to hard-cancel on timeout
        (rare — would only happen if a single _flush_batch hangs > 5 s).
        """
        if final_status is not None:
            self.status = final_status

        # Cooperative writer shutdown — flag + wait.
        self._shutting_down = True
        if self._writer_task is not None and not self._writer_task.done():
            try:
                await asyncio.wait_for(self._writer_task, timeout=5.0)
            except asyncio.TimeoutError:
                log.warning(
                    "close: writer didn't drain in 5s, hard-cancelling run=%s",
                    self.run_id,
                )
                self._writer_task.cancel()
                try:
                    await self._writer_task
                except (asyncio.CancelledError, BaseException):
                    pass
            except (asyncio.CancelledError, BaseException):
                pass
            self._writer_task = None

        # Final DB update with completion timestamp + status. Wrapped in
        # try/except so a store hiccup doesn't crash the close path.
        # Held under the same write lock as the flush path so we don't
        # race with the writer's last in-flight update.
        if self._db_write_lock is not None:
            try:
                async with self._db_write_lock:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None,
                        self.store.update_agent_run,
                        self.run_id,
                        {
                            "status": self.status,
                            "completed_at": datetime.now(timezone.utc),
                            "total_input_tokens": self.total_input_tokens,
                            "total_output_tokens": self.total_output_tokens,
                            "last_event_seq": self.event_seq - 1
                            if self.event_seq > 0
                            else 0,
                        },
                    )
            except Exception:
                log.exception(
                    "close: final agent_run update failed run=%s", self.run_id
                )

        # Cancel any pending HITL futures so awaiting coroutines fail fast.
        for fut in list(self.pending_approvals.values()):
            if not fut.done():
                fut.cancel()
        self.pending_approvals.clear()
        for fut in list(self.pending_questions.values()):
            if not fut.done():
                fut.cancel()
        self.pending_questions.clear()

        # Wake any subscribers blocked on get() so their generators exit.
        for q in list(self.subscribers):
            try:
                q.put_nowait({"type": "done", "seq": -1, "synthetic": True})
            except asyncio.QueueFull:
                pass
        self.subscribers.clear()

    # ------------------------------------------------------------------
    # Emit
    # ------------------------------------------------------------------

    async def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        force_flush: bool | None = None,
    ) -> dict:
        """Emit one event to all consumers + persist to DB.

        Returns the assembled event dict (caller may want the seq for
        approval_id correlation etc.).

        ``force_flush=None`` (default) routes through the critical-event
        set: known-critical types persist synchronously, everything else
        batches. Pass ``True`` / ``False`` to override per-call.
        """
        seq = self.event_seq
        self.event_seq += 1
        event = {
            "seq": seq,
            "type": event_type,
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "depth": self.depth,
            "ts": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
        }

        # 1. In-memory ring buffer.
        self.event_buffer.append(event)
        # Soft-trim when over buffer_size — drop oldest. Subscribers that
        # have not consumed up to that point will need to fall through to
        # DB on reconnect (acceptable; DB has everything).
        while len(self.event_buffer) > self.buffer_size:
            self.event_buffer.popleft()

        # 2. Push to live subscribers.
        #
        # Drop policy when a subscriber's queue is full:
        #   - High-frequency low-value events (token, thought): drop silently,
        #     count for telemetry. The client can reconstruct via /stream
        #     since=N from DB if needed.
        #   - Everything else (tool_start, tool_end, citation, ask_human,
        #     approval_request, sub_agent_*, done, ...): rotate the queue —
        #     drop the OLDEST queued event to make room. Critical
        #     transitions never silently vanish; the client sees a seq
        #     gap and can refetch from DB.
        #
        # Investigation (Inc 7 long-task probe, task A): with the original
        # maxsize=200 + universal drop-on-full, an SSE subscriber lost
        # 89% of events on a 45-tool-call run because token bursts filled
        # the queue faster than the network drained it. New maxsize=5000
        # + rotate-on-full for non-token events virtually eliminates the
        # loss for realistic runs.
        _LOSSY_TYPES = ("token", "thought")
        if self.subscribers:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    if event_type in _LOSSY_TYPES:
                        self._dropped_token_events += 1
                        if self._dropped_token_events % 100 == 1:
                            log.warning(
                                "subscriber queue full (run=%s seq=%d): dropped "
                                "%d high-frequency events so far; client will "
                                "see seq gaps and can refetch from DB",
                                self.run_id, seq, self._dropped_token_events,
                            )
                    else:
                        # Rotate: pop oldest, push new. Loses one queued
                        # event but ensures the critical-ish new one
                        # reaches the subscriber.
                        try:
                            dropped = q.get_nowait()
                            log.warning(
                                "subscriber queue full (run=%s): rotated out "
                                "seq=%d (%s) to make room for seq=%d (%s)",
                                self.run_id,
                                dropped.get("seq"),
                                dropped.get("type"),
                                seq,
                                event_type,
                            )
                            try:
                                q.put_nowait(event)
                            except asyncio.QueueFull:
                                pass
                        except asyncio.QueueEmpty:
                            pass

        # 3. Persist.
        should_force = (
            force_flush if force_flush is not None
            else event_type in _CRITICAL_EVENT_TYPES
        )
        db_row = {
            "run_id": self.run_id,
            "seq": seq,
            "event_type": event_type,
            "payload_json": event["payload"],
        }
        if should_force:
            # Synchronous write — blocks until DB has it.
            await self._persist_one(db_row)
        else:
            assert self._db_queue is not None, "handle.start() not called"
            self._db_queue.put_nowait(db_row)

        return event

    # ------------------------------------------------------------------
    # Subscribe (reconnect-capable)
    # ------------------------------------------------------------------

    async def subscribe(
        self, since_seq: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield events with seq>since. Replays buffer/DB first, then
        tails live events until the run reaches a terminal state.

        Reconnect protocol — caller (the /stream SSE route) is supposed
        to track the highest seq seen and pass it as ``since_seq`` on
        the next connection. Server fills the gap from the in-memory
        buffer (fast path) or DB (slow path) then tails live.
        """
        # ── Phase 1: replay missed events ─────────────────────────────
        replayed_max = since_seq
        oldest_in_buffer = (
            self.event_buffer[0]["seq"] if self.event_buffer else self.event_seq
        )

        if since_seq + 1 >= oldest_in_buffer:
            # Buffer covers the gap — replay from memory.
            for ev in list(self.event_buffer):
                if ev["seq"] > since_seq:
                    yield ev
                    replayed_max = max(replayed_max, ev["seq"])
        else:
            # Buffer too old — fall through to DB, then catch up with buffer.
            try:
                rows = self.store.list_agent_events_since(
                    self.run_id, since_seq=since_seq
                )
            except Exception:
                log.exception(
                    "subscribe: DB replay failed run=%s since=%d",
                    self.run_id,
                    since_seq,
                )
                rows = []
            for r in rows:
                ev = {
                    "seq": r["seq"],
                    "type": r["event_type"],
                    "run_id": self.run_id,
                    "conversation_id": self.conversation_id,
                    "depth": self.depth,
                    "ts": r["created_at"].isoformat()
                    if hasattr(r["created_at"], "isoformat")
                    else str(r["created_at"]),
                    "payload": r["payload_json"] or {},
                }
                yield ev
                replayed_max = max(replayed_max, ev["seq"])
            # Catch-up: events between DB high-water and current buffer.
            for ev in list(self.event_buffer):
                if ev["seq"] > replayed_max:
                    yield ev
                    replayed_max = max(replayed_max, ev["seq"])

        # ── Phase 2: tail live (if run still active) ──────────────────
        if self.status in {"done", "failed", "interrupted"}:
            return  # nothing more will come

        # maxsize tuned from Inc 7 probe (Task A): 200 dropped 89% of events
        # on 45-tool-call runs. 5000 = ~25× headroom for typical streaming
        # rates (~20 tok/s) over multi-second network gaps. Memory cost is
        # negligible (events ~500B = 2.5MB max per subscriber).
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subscribers.add(q)
        # Idle keepalive: yield a synthetic ``_keepalive`` sentinel after
        # ``_KEEPALIVE_S`` seconds of queue silence so the SSE route can
        # flush a ``: ping`` comment without idle proxies/SSH tunnels
        # tearing the connection down. Doing the timeout against the
        # local queue (rather than against ``subscribe.__anext__()`` from
        # the route via ``asyncio.wait_for``) avoids the round-3 Task J
        # bug: ``wait_for`` cancels the inner coroutine on each timeout,
        # which propagates into this generator's ``await q.get()`` and
        # tears the generator down via its finally — every subsequent
        # ``__anext__`` then raises StopAsyncIteration, closing the
        # client's stream after the FIRST quiet 15s window.
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield {"type": "_keepalive", "seq": -1, "synthetic": True}
                    continue
                if ev.get("synthetic"):
                    # close() broadcast a fake terminal — exit cleanly.
                    return
                if ev["seq"] <= replayed_max:
                    # Race: subscriber added between buffer replay and
                    # the emit that put this event in our queue. Skip
                    # the dup, replayed_max ensures monotonicity.
                    continue
                yield ev
                replayed_max = ev["seq"]
                if ev["type"] in {"done", "interrupted", "error"}:
                    return
        finally:
            self.subscribers.discard(q)

    # ------------------------------------------------------------------
    # HITL waiters (Inc 4 wires the runtime side; the primitives live here)
    # ------------------------------------------------------------------

    async def wait_for_approval(
        self, approval_id: str, *, timeout_s: float = 600.0
    ) -> FeedbackEnvelope:
        """Block until /feedback fulfils this approval_id, or timeout.

        Timeout = synthesise a 'deny' envelope with message='timeout'
        so the agent can continue (the SDK's PreToolUse hook expects
        a definite allow/deny, not an exception).

        Early-arrival handling: if /feedback already landed before this
        coroutine had a chance to register a Future (a fast external
        responder reacting to the emit's SSE before emit returns), the
        envelope was stashed in ``early_approvals`` — drain it here so
        we don't block waiting for a response that already came."""
        early = self.early_approvals.pop(approval_id, None)
        if early is not None:
            return early
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_approvals[approval_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError:
            return FeedbackEnvelope(
                type="deny", approval_id=approval_id, message="timeout"
            )
        finally:
            self.pending_approvals.pop(approval_id, None)

    async def wait_for_answer(
        self, question_id: str, *, timeout_s: float = 24 * 3600
    ) -> str:
        """Block until /feedback fulfils this question_id with an
        'answer' envelope. Raises ``TimeoutError`` on timeout — the
        runtime catches it and aborts the run with status=failed.

        Early-arrival handling: drain ``early_answers[question_id]``
        first; if /feedback landed before registration (fast external
        responder racing emit), the envelope is already waiting."""
        early = self.early_answers.pop(question_id, None)
        if early is not None:
            return early.message or ""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_questions[question_id] = fut
        try:
            env = await asyncio.wait_for(fut, timeout=timeout_s)
            return env.message or ""
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"ask_human {question_id} timed out after {timeout_s}s"
            )
        finally:
            self.pending_questions.pop(question_id, None)

    def submit_feedback(self, fb: FeedbackEnvelope) -> None:
        """Called by /feedback route. Resolves any matching pending
        approval / question future + enqueues the envelope on user_inbox
        so the agent loop can pick up interrupt/redirect at its next
        checkpoint.

        Synchronous: it only puts on asyncio queues + sets futures,
        which is safe from the request handler coroutine."""
        # Match-and-resolve approvals/answers immediately so the agent's
        # await wakes up without going through the inbox drain cycle.
        # Buffer early arrivals (envelope landed before the agent's
        # wait_for_* registered its Future) — wait_for_* drains the
        # buffer at registration time. Otherwise the envelope is lost
        # and the agent blocks for the full tool timeout.
        if fb.type in ("approve", "deny") and fb.approval_id:
            fut = self.pending_approvals.get(fb.approval_id)
            if fut is not None and not fut.done():
                fut.set_result(fb)
            else:
                self.early_approvals[fb.approval_id] = fb
        elif fb.type == "answer" and fb.question_id:
            fut = self.pending_questions.get(fb.question_id)
            if fut is not None and not fut.done():
                fut.set_result(fb)
            else:
                self.early_answers[fb.question_id] = fb

        # Always also push on the inbox — interrupt / message types
        # consumed there, and approval/answer types still recorded
        # for the agent loop's audit if it cares.
        if self.user_inbox is not None:
            try:
                self.user_inbox.put_nowait(fb)
            except asyncio.QueueFull:
                log.warning(
                    "user_inbox full, dropping feedback run=%s type=%s",
                    self.run_id,
                    fb.type,
                )

    # ------------------------------------------------------------------
    # Budget tracking (Inc 5 calls these from usage events)
    # ------------------------------------------------------------------

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

    def is_over_budget(self) -> bool:
        if self.token_budget_total is None:
            return False
        return (self.total_input_tokens + self.total_output_tokens) >= self.token_budget_total

    # ------------------------------------------------------------------
    # DB writer (background coroutine)
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        """Batch DB writes: every 100ms OR 50 events, whichever first.

        Cooperative shutdown: when close() sets ``_shutting_down=True``,
        the loop drains the queue one final pass, flushes the tail, and
        exits cleanly. Avoids the cancel-mid-flush trap where the
        writer's in-flight batch (already pulled from the queue but not
        yet committed) would be lost.

        Hard cancellation (CancelledError) is still handled as a
        fallback — for shutdown paths that can't use the cooperative
        flag — but loses any in-flight batch.
        """
        assert self._db_queue is not None
        batch: list[dict] = []
        try:
            while True:
                if self._shutting_down and self._db_queue.empty() and not batch:
                    # Cooperative exit: nothing more coming, nothing pending.
                    return
                try:
                    first = await asyncio.wait_for(
                        self._db_queue.get(), timeout=self.batch_interval_s
                    )
                    batch.append(first)
                    while len(batch) < self.batch_size:
                        try:
                            batch.append(self._db_queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    pending = batch
                    batch = []
                    await self._flush_batch(pending)
                except asyncio.TimeoutError:
                    if batch:
                        pending = batch
                        batch = []
                        await self._flush_batch(pending)
        except asyncio.CancelledError:
            # Hard cancel — best-effort tail drain (in-flight batch may
            # have been lost mid-flush; cooperative shutdown is the
            # preferred path).
            tail: list[dict] = []
            while True:
                try:
                    tail.append(self._db_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if tail:
                try:
                    await self._flush_batch(tail)
                except Exception:
                    log.exception(
                        "writer_loop: final drain flush failed run=%s",
                        self.run_id,
                    )
            raise

    async def _drain_and_flush(self) -> None:
        """Sync helper called by close() before cancelling the writer.
        Pulls everything queued + persists in one shot."""
        assert self._db_queue is not None
        batch: list[dict] = []
        while True:
            try:
                batch.append(self._db_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._flush_batch(batch)

    async def _flush_batch(self, batch: list[dict]) -> None:
        """Bulk-insert a batch + bump last_event_seq on the run row.
        Both DB calls happen in a thread executor since the store is
        sync (SQLAlchemy session); offloading keeps the event loop free.

        Serialized by ``_db_write_lock`` so concurrent _flush_batch +
        _persist_one calls don't race on the same SQLite connection.
        """
        if not batch:
            return
        loop = asyncio.get_running_loop()
        assert self._db_write_lock is not None
        async with self._db_write_lock:
            try:
                await loop.run_in_executor(
                    None, self.store.bulk_append_agent_events, batch
                )
                max_seq = max(r["seq"] for r in batch)
                await loop.run_in_executor(
                    None,
                    self.store.update_agent_run,
                    self.run_id,
                    {"last_event_seq": max_seq},
                )
            except Exception:
                log.exception(
                    "writer_loop: batch flush failed run=%s batch=%d (events lost from DB but still in buffer)",
                    self.run_id,
                    len(batch),
                )

    async def _persist_one(self, row: dict) -> None:
        """Synchronous DB write for force_flush. Used for critical events
        where we MUST be sure the event is durable before returning.

        Same lock as _flush_batch — a critical event arriving during a
        batch flush waits for the batch to commit. Acceptable: critical
        events are rare (approval / ask_human / done / interrupted /
        error), and the lock wait is dominated by SQLite commit latency
        anyway."""
        loop = asyncio.get_running_loop()
        assert self._db_write_lock is not None
        async with self._db_write_lock:
            try:
                await loop.run_in_executor(None, self.store.append_agent_event, row)
                await loop.run_in_executor(
                    None,
                    self.store.update_agent_run,
                    self.run_id,
                    {"last_event_seq": row["seq"]},
                )
            except Exception:
                log.exception(
                    "persist_one: critical event write failed run=%s seq=%d type=%s",
                    self.run_id,
                    row["seq"],
                    row["event_type"],
                )
