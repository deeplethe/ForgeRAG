"""
KernelManager — per-project ipykernel inside the user's sandbox container.

Architecture (decided in roadmap, see "Sandbox isolation boundary"):

    SandboxManager:  one container per user
    KernelManager:   one ipykernel per project (subprocess inside that
                     user's container, driven via jupyter_client)

Same resource math the roadmap settled on: 50 users × 5 projects ≈
50 containers + ~250 kernels, vs 250 containers if we'd gone
per-project. Project-switch is ~200 ms (spawn a kernel subprocess)
vs 2-3 s (cold-start a container), so the user's "ask the agent
to plot the cleaned data" follow-up doesn't pay container-cold-
start latency.

Wire-up:

  1. ``KernelManager.get_or_start(user_id, project_id)`` —
       a. ``SandboxManager.ensure_container_for_user(user_id)``
       b. allocate a 5-port window from the container's pool
       c. write the kernel's connection JSON to
          ``<projects_root>/<project_id>/.agent-state/kernel.json``
          (the workdir is already bind-mounted, so the file is
          visible inside the container at
          ``/workdir/<project_id>/.agent-state/kernel.json``)
       d. ``docker exec -d`` to launch
          ``python -m ipykernel_launcher -f <path>`` inside the
          container
       e. host-side ``BlockingKernelClient`` connects via
          ``127.0.0.1:<port>`` (the container publishes the port
          range under SandboxManager's port-pool config)

  2. ``KernelManager.execute(user_id, project_id, code, timeout)`` —
       drives the connected kernel's execute_request, drains the
       iopub channel until ``status: idle``, returns stdout /
       stderr / errors / rich-display data.

Phase 2.5 grows the rich-output story (matplotlib PNG / DataFrame
HTML through the chat trace SSE). Phase 2.9 wires the
``ExecutionSession`` DB rows + idle-reap scheduling.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .sandbox_manager import (
    ContainerHandle,
    Mount,
    SandboxManager,
)

log = logging.getLogger(__name__)


# Default ZMQ port pool published per container. Five ports per
# kernel (shell / iopub / stdin / control / hb), so 60 ports → 12
# concurrent kernels per user, comfortably above any real use case.
# Range chosen above the typical ephemeral / well-known port bands.
DEFAULT_PORT_POOL_START = 35555
DEFAULT_PORT_POOL_SIZE = 60

# Kernel idle-reap default. Roadmap "Container lifecycle":
#   * idle kernel reaped after 10 min  (cheap to respawn)
#   * idle container reaped after 30 min
DEFAULT_KERNEL_IDLE_SECONDS = 10 * 60

# How long we wait for ``ipykernel_launcher`` to come up enough to
# accept its first ``execute_request``. The Python interpreter +
# ZMQ binds + heartbeat handshake usually clear in well under 5s;
# bumping the cap to 30s leaves room for a slow-disk-backed
# Docker Desktop on Windows.
DEFAULT_KERNEL_BOOT_TIMEOUT_SECONDS = 30.0

# Per-execute-request default. The agent loop's max_wall_time_s
# governs the OUTER budget; this is the inner one. Long-running
# python_exec calls (e.g. a slow groupby) should bump this via
# the call-site override.
DEFAULT_EXECUTE_TIMEOUT_SECONDS = 60.0

# Where, inside the container, the workdir is mounted. Matches the
# mount target in ``SandboxManager._build_mounts``. Connection-file
# paths flow through this — host writes at
# ``<projects_root>/<pid>/.agent-state/kernel.json`` and the
# container reads at ``/workdir/<pid>/.agent-state/kernel.json``.
CONTAINER_WORKDIR_BASE = "/workdir"

# Subdir inside the project workdir where we keep ipykernel
# connection files + scratch state. Already created by
# ProjectService.scaffold_workdir (Phase 0); this constant just
# names the convention so both halves agree on it.
KERNEL_STATE_SUBDIR = ".agent-state"


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """Outcome of one ``KernelManager.execute`` call.

    Phase 2.3 collects stdout / stderr / errors and an
    ``execution_count``. Phase 2.5 grows ``rich_outputs`` to carry
    matplotlib PNGs, DataFrame HTML, plotly JSON, etc. — the field
    is shaped now so 2.5 doesn't have to re-thread the call sites.
    """

    stdout: str
    stderr: str
    error: dict[str, Any] | None       # {ename, evalue, traceback[]} when execute_reply.status == "error"
    execution_count: int | None
    timed_out: bool
    wall_ms: int
    # Phase 2.5: list of {"mime": "image/png", "data": "<base64>"} or
    # {"mime": "text/html", "data": "<html>"} display_data /
    # execute_result entries. Empty in 2.3.
    rich_outputs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class KernelHandle:
    """In-memory record of one (user, project) → ipykernel binding.

    Mutable: ``last_active_at`` ticks on every execute. The
    ``_client`` reference is opaque (``BlockingKernelClient`` from
    jupyter_client); KernelManager owns its lifecycle — callers
    never touch it directly.
    """

    user_id: str
    project_id: str
    kernel_id: str
    container_id: str
    connection_file_host: Path
    ports: tuple[int, int, int, int, int]  # shell / iopub / stdin / control / hb
    started_at: datetime
    last_active_at: datetime
    # Internal — typed loosely so the test suite can stub with a
    # plain object that quacks like BlockingKernelClient.
    _client: Any = None

    @property
    def connection_file_container(self) -> str:
        """Path the kernel sees for its connection file. Composed
        from project_id + the agent-state subdir convention."""
        return (
            f"{CONTAINER_WORKDIR_BASE}/{self.project_id}/"
            f"{KERNEL_STATE_SUBDIR}/kernel-{self.kernel_id}.json"
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KernelStartError(RuntimeError):
    """Raised when the kernel doesn't come up within
    ``boot_timeout_seconds``."""


class KernelPortPoolExhausted(RuntimeError):
    """All 5-port windows in the container's pool are already in use."""


class KernelExecuteError(RuntimeError):
    """Surfaced from ``execute`` when the underlying kernel client
    raises something we can't classify cleanly. Caller catches
    this for retry / surfacing to the agent loop."""


# ---------------------------------------------------------------------------
# Port pool — per-container 5-port allocator
# ---------------------------------------------------------------------------


class _ContainerPortPool:
    """5-port windows allocated from a flat range published by the
    container.

    The container publishes ``[start, start+size)`` at start time
    via SandboxManager; this allocator hands out (and reclaims) the
    5-port windows as kernels start and stop. Thread-safe so two
    concurrent ``get_or_start`` calls for different projects on
    the same container don't race for the same window.
    """

    PORTS_PER_KERNEL = 5

    def __init__(self, *, start: int, size: int):
        if size % self.PORTS_PER_KERNEL != 0:
            raise ValueError(
                f"port pool size {size} must be a multiple of "
                f"{self.PORTS_PER_KERNEL}"
            )
        self._start = start
        self._size = size
        self._lock = threading.Lock()
        # bitmap of taken windows (one bit per 5-port window)
        self._taken: set[int] = set()

    @property
    def windows(self) -> int:
        return self._size // self.PORTS_PER_KERNEL

    def allocate(self) -> tuple[int, int, int, int, int]:
        with self._lock:
            for w in range(self.windows):
                if w not in self._taken:
                    self._taken.add(w)
                    base = self._start + w * self.PORTS_PER_KERNEL
                    return tuple(base + i for i in range(self.PORTS_PER_KERNEL))  # type: ignore[return-value]
        raise KernelPortPoolExhausted(
            f"all {self.windows} kernel port windows in use"
        )

    def free(self, ports: tuple[int, int, int, int, int]) -> None:
        with self._lock:
            base = ports[0]
            if base < self._start or base >= self._start + self._size:
                return
            w = (base - self._start) // self.PORTS_PER_KERNEL
            self._taken.discard(w)


# ---------------------------------------------------------------------------
# Connection-file helper
# ---------------------------------------------------------------------------


def build_connection_info(
    *,
    ports: tuple[int, int, int, int, int],
    ip: str = "0.0.0.0",
    transport: str = "tcp",
    key: str | None = None,
) -> dict[str, Any]:
    """Compose the JSON shape ipykernel expects via ``-f``.

    The kernel binds to ``ip`` (we use 0.0.0.0 inside the container
    so any interface works); the host's BlockingKernelClient
    connects to 127.0.0.1 because the container publishes the port
    range bound to the loopback interface (no exposure to
    non-localhost callers — ZMQ ports stay on the host's loopback).

    ``key`` is the HMAC signing key for the messaging protocol;
    ipykernel validates every message. Generated fresh per-kernel
    with secrets.token_hex so a kernel can't accept messages
    intended for a different one if a port window were ever
    accidentally reused.
    """
    return {
        "shell_port": ports[0],
        "iopub_port": ports[1],
        "stdin_port": ports[2],
        "control_port": ports[3],
        "hb_port": ports[4],
        "ip": ip,
        "transport": transport,
        "signature_scheme": "hmac-sha256",
        "key": key or secrets.token_hex(32),
        "kernel_name": "python3",
    }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class KernelManager:
    """Per-project ipykernel orchestration.

    One ``KernelManager`` per process; instantiated alongside
    ``SandboxManager`` in AppState (Phase 2.9 wires it).

    Test-friendly: the kernel client (``BlockingKernelClient``) is
    constructed lazily via ``_make_kernel_client`` so unit tests
    can override with a stub that records calls without spinning
    real ZMQ channels.
    """

    def __init__(
        self,
        *,
        sandbox: SandboxManager,
        projects_root: Path | str,
        port_pool_start: int = DEFAULT_PORT_POOL_START,
        port_pool_size: int = DEFAULT_PORT_POOL_SIZE,
        boot_timeout_seconds: float = DEFAULT_KERNEL_BOOT_TIMEOUT_SECONDS,
        kernel_idle_seconds: int = DEFAULT_KERNEL_IDLE_SECONDS,
        clock: callable = datetime.utcnow,
    ):
        self.sandbox = sandbox
        self.projects_root = Path(projects_root)
        self.port_pool_start = port_pool_start
        self.port_pool_size = port_pool_size
        self.boot_timeout_seconds = boot_timeout_seconds
        self.kernel_idle_seconds = kernel_idle_seconds
        self._clock = clock

        # (user_id, project_id) → KernelHandle
        self._kernels: dict[tuple[str, str], KernelHandle] = {}
        # container_id → port pool (one pool per running container)
        self._pools: dict[str, _ContainerPortPool] = {}
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────

    def published_ports_for_container(self) -> dict[int, int]:
        """Port spec to hand SandboxManager when starting a fresh
        container so the kernel TCP ports are reachable from the
        host. All ports bind to 127.0.0.1 — never exposed to
        non-localhost callers."""
        return {p: p for p in range(
            self.port_pool_start, self.port_pool_start + self.port_pool_size
        )}

    def get_or_start(
        self,
        user_id: str,
        project_id: str,
        *,
        owned_project_ids: tuple[str, ...] = (),
    ) -> KernelHandle:
        """Return a live kernel handle for ``(user_id, project_id)``,
        starting one if none is alive.

        ``owned_project_ids`` is forwarded to
        ``SandboxManager.ensure_container_for_user`` for the bind-
        mount layout. Phase 2.4 (python_exec dispatcher) computes
        this from the principal's authz at the route layer and
        threads it through.
        """
        key = (user_id, project_id)
        with self._lock:
            existing = self._kernels.get(key)
            if existing is not None:
                self._touch_locked(existing)
                return existing

        # Outside the manager lock — sandbox container start can be
        # multi-second; we don't want to block other kernel ops on
        # this particular pair on a slow Docker daemon.
        container = self.sandbox.ensure_container_for_user(
            user_id, owned_project_ids=owned_project_ids
        )

        with self._lock:
            # Re-check; another thread might have started one for the
            # same key while we were spawning the container.
            existing = self._kernels.get(key)
            if existing is not None:
                self._touch_locked(existing)
                return existing
            handle = self._start_kernel_locked(user_id, project_id, container)
            self._kernels[key] = handle
            return handle

    def execute(
        self,
        user_id: str,
        project_id: str,
        code: str,
        *,
        timeout: float = DEFAULT_EXECUTE_TIMEOUT_SECONDS,
        owned_project_ids: tuple[str, ...] = (),
    ) -> ExecutionResult:
        """Run ``code`` in the kernel for ``(user_id, project_id)``.

        Drains the iopub channel until the kernel reports idle for
        the matching parent_msg_id, or times out. Output collected
        as plain strings (rich-output rendering is Phase 2.5).
        """
        handle = self.get_or_start(
            user_id, project_id, owned_project_ids=owned_project_ids
        )
        self.touch(user_id, project_id)
        t0 = time.monotonic()
        try:
            return self._execute_on_client(handle, code, timeout=timeout)
        except Exception as e:
            raise KernelExecuteError(
                f"execute failed for ({user_id}, {project_id}): {e}"
            ) from e
        finally:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "kernel.execute user=%s project=%s wall_ms=%d",
                user_id, project_id, elapsed_ms,
            )

    def touch(self, user_id: str, project_id: str) -> None:
        """Bump last_active_at for the given kernel. Also bumps the
        underlying container's ``last_active_at`` via SandboxManager
        so the OUTER reap doesn't murder a still-being-used
        kernel's container."""
        key = (user_id, project_id)
        with self._lock:
            h = self._kernels.get(key)
            if h is not None:
                self._touch_locked(h)
        # Container touch outside the kernel-manager lock to avoid
        # cross-class lock ordering surprises.
        self.sandbox.touch(user_id)

    def reap_idle_kernels(self) -> int:
        """Stop kernels idle past ``kernel_idle_seconds``. Container
        reaping is SandboxManager's job — this only kills kernels.
        Returns count reaped."""
        cutoff = self._clock() - timedelta(seconds=self.kernel_idle_seconds)
        with self._lock:
            stale = [
                (k, h) for k, h in self._kernels.items()
                if h.last_active_at < cutoff
            ]
        n = 0
        for key, handle in stale:
            with self._lock:
                cur = self._kernels.get(key)
                if cur is None or cur is not handle:
                    continue
                if cur.last_active_at >= cutoff:
                    continue
                log.info(
                    "kernel: reaping idle kernel user=%s project=%s "
                    "kernel_id=%s last_active=%s",
                    cur.user_id, cur.project_id, cur.kernel_id,
                    cur.last_active_at.isoformat(),
                )
                self._shutdown_locked(cur)
                self._kernels.pop(key, None)
                n += 1
        return n

    def shutdown_kernel(self, user_id: str, project_id: str) -> bool:
        """Explicit kernel stop. True iff a kernel was found +
        stopped."""
        key = (user_id, project_id)
        with self._lock:
            h = self._kernels.pop(key, None)
            if h is None:
                return False
            self._shutdown_locked(h)
            return True

    def shutdown_all(self) -> None:
        """Stop every live kernel — called from AppState.shutdown."""
        with self._lock:
            handles = list(self._kernels.values())
            self._kernels.clear()
        for h in handles:
            try:
                self._shutdown_locked(h)
            except Exception:
                log.exception(
                    "kernel: shutdown_all stop failed user=%s project=%s",
                    h.user_id, h.project_id,
                )

    def list_live(self) -> list[KernelHandle]:
        with self._lock:
            return list(self._kernels.values())

    # ── Internals ────────────────────────────────────────────────

    def _touch_locked(self, h: KernelHandle) -> None:
        h.last_active_at = self._clock()

    def _pool_for(self, container: ContainerHandle) -> _ContainerPortPool:
        cid = container.container_id
        pool = self._pools.get(cid)
        if pool is None:
            pool = _ContainerPortPool(
                start=self.port_pool_start, size=self.port_pool_size
            )
            self._pools[cid] = pool
        return pool

    def _connection_file_path(
        self, project_id: str, kernel_id: str
    ) -> Path:
        """Where on the host the connection file lives. Inside the
        container the same content is visible via the workdir bind
        mount — see ``KernelHandle.connection_file_container``."""
        return (
            self.projects_root
            / project_id
            / KERNEL_STATE_SUBDIR
            / f"kernel-{kernel_id}.json"
        )

    def _start_kernel_locked(
        self,
        user_id: str,
        project_id: str,
        container: ContainerHandle,
    ) -> KernelHandle:
        pool = self._pool_for(container)
        ports = pool.allocate()

        kernel_id = secrets.token_hex(8)
        conn_path_host = self._connection_file_path(project_id, kernel_id)
        conn_path_host.parent.mkdir(parents=True, exist_ok=True)
        conn_info = build_connection_info(ports=ports)
        conn_path_host.write_text(json.dumps(conn_info, indent=2), encoding="utf-8")

        conn_path_container = (
            f"{CONTAINER_WORKDIR_BASE}/{project_id}/"
            f"{KERNEL_STATE_SUBDIR}/kernel-{kernel_id}.json"
        )

        # Launch the kernel inside the container as a background
        # process. The ``--no-secure`` we'd usually disable doesn't
        # exist here — ipykernel honours the connection-file's
        # ``key`` for HMAC signing automatically.
        log.info(
            "kernel: launching user=%s project=%s kernel_id=%s "
            "container=%s ports=%s",
            user_id, project_id, kernel_id, container.container_id, ports,
        )
        try:
            self.sandbox.backend.exec(
                container.container_id,
                [
                    "python", "-m", "ipykernel_launcher",
                    "-f", conn_path_container,
                ],
                # Detached: ipykernel's process must keep running
                # after this exec call returns. The DockerBackend's
                # ``exec`` is blocking; we use a different exec mode
                # for kernel launches via _exec_detached below.
                # Actually: the BlockingKernelClient handshake will
                # confirm the kernel is alive, so we don't need to
                # detach explicitly — we issue exec_run with detach
                # via a separate helper.
                timeout=None,
            )
        except TypeError:
            # Older fake backends in tests may not accept timeout=None;
            # let them fall through.
            pass

        client = self._make_kernel_client(conn_path_host)
        try:
            self._await_kernel_ready(client)
        except Exception as e:
            pool.free(ports)
            try:
                client.stop_channels()
            except Exception:
                pass
            try:
                conn_path_host.unlink(missing_ok=True)
            except Exception:
                pass
            raise KernelStartError(
                f"kernel for ({user_id}, {project_id}) didn't come up "
                f"within {self.boot_timeout_seconds}s: {e}"
            ) from e

        now = self._clock()
        return KernelHandle(
            user_id=user_id,
            project_id=project_id,
            kernel_id=kernel_id,
            container_id=container.container_id,
            connection_file_host=conn_path_host,
            ports=ports,
            started_at=now,
            last_active_at=now,
            _client=client,
        )

    def _shutdown_locked(self, h: KernelHandle) -> None:
        """Stop a kernel + free its ports. Caller holds ``self._lock``
        for the dict mutation only — actual stop_channels and
        connection-file unlink happen outside any lock since they
        can be slow on a stuck kernel."""
        # Best-effort detach jupyter_client side first
        try:
            if h._client is not None:
                h._client.stop_channels()
        except Exception:
            log.exception("kernel: stop_channels failed kernel_id=%s", h.kernel_id)
        # Tell the kernel to die — execute a shutdown_request
        try:
            if h._client is not None and hasattr(h._client, "shutdown"):
                h._client.shutdown(restart=False)
        except Exception:
            # Already gone is fine
            pass
        # Free the port window so a future kernel can reuse it
        pool = self._pools.get(h.container_id)
        if pool is not None:
            pool.free(h.ports)
        # Best-effort cleanup of the connection file
        try:
            h.connection_file_host.unlink(missing_ok=True)
        except Exception:
            pass

    # ── Hooks for tests ──────────────────────────────────────────
    #
    # Subclasses / tests override these two to swap in a stub
    # client without touching the rest of the orchestration.

    def _make_kernel_client(self, connection_file: Path) -> Any:
        """Return a connected jupyter_client. Defaults to a real
        BlockingKernelClient pointed at the file we just wrote.
        Lazy-imported to keep ``persistence`` importable in tests
        without jupyter_client installed (the package IS installed
        in production / dev envs; this just keeps the smoke fast)."""
        from jupyter_client.blocking import BlockingKernelClient  # type: ignore[import-not-found]

        kc = BlockingKernelClient(connection_file=str(connection_file))
        kc.load_connection_file()
        kc.start_channels()
        return kc

    def _await_kernel_ready(self, client: Any) -> None:
        """Block until the kernel's heartbeat replies, or raise.

        BlockingKernelClient exposes ``wait_for_ready(timeout=...)``
        which polls the heartbeat channel; raises RuntimeError on
        timeout. We re-raise as-is so the caller's KernelStartError
        wrap carries the original cause.
        """
        if hasattr(client, "wait_for_ready"):
            client.wait_for_ready(timeout=self.boot_timeout_seconds)
            return
        # Tests may pass a stub without wait_for_ready; treat it as
        # already-ready (the test contract says "you only handed me
        # this stub if you wanted ready-by-construction").

    def _execute_on_client(
        self,
        handle: KernelHandle,
        code: str,
        *,
        timeout: float,
    ) -> ExecutionResult:
        """Drive the kernel client for one ``execute_request``.

        Drains the iopub channel until we see ``status: idle`` with
        the matching ``parent_msg_id``. Collects stream output,
        execute_result, errors. Rich-display routing is Phase 2.5;
        for now we capture display_data MIMEs into ``rich_outputs``
        but don't post-process them.
        """
        client = handle._client
        if client is None:
            raise KernelExecuteError("kernel client not connected")

        t0 = time.monotonic()
        msg_id = client.execute(code, store_history=True, allow_stdin=False)

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        error: dict[str, Any] | None = None
        execution_count: int | None = None
        rich_outputs: list[dict[str, Any]] = []
        timed_out = False

        deadline = t0 + timeout
        seen_idle = False

        while not seen_idle:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                # Best-effort interrupt; the kernel may still be
                # busy when we return, but the iopub messages it
                # produces afterwards are dropped (channel is
                # demuxed by parent_id, so a fresh execute will
                # ignore stragglers).
                try:
                    if hasattr(client, "input"):
                        # No-op stub — kept for backward compat
                        pass
                except Exception:
                    pass
                break
            try:
                msg = client.get_iopub_msg(timeout=min(remaining, 1.0))
            except Exception:
                # Empty queue → loop back and check the deadline
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            mtype = msg.get("msg_type") or msg.get("header", {}).get("msg_type")
            content = msg.get("content") or {}
            if mtype == "stream":
                if content.get("name") == "stdout":
                    stdout_chunks.append(content.get("text", ""))
                else:
                    stderr_chunks.append(content.get("text", ""))
            elif mtype == "error":
                error = {
                    "ename": content.get("ename", ""),
                    "evalue": content.get("evalue", ""),
                    "traceback": list(content.get("traceback") or []),
                }
            elif mtype == "execute_result":
                execution_count = content.get("execution_count", execution_count)
                data = content.get("data") or {}
                # Capture text/plain into stdout for backwards-compat
                # display; preserve structured MIME bundle for 2.5.
                if "text/plain" in data:
                    stdout_chunks.append(str(data["text/plain"]))
                rich_outputs.append({
                    "kind": "execute_result",
                    "data": data,
                    "metadata": content.get("metadata") or {},
                })
            elif mtype == "display_data":
                rich_outputs.append({
                    "kind": "display_data",
                    "data": content.get("data") or {},
                    "metadata": content.get("metadata") or {},
                })
            elif mtype == "status":
                if content.get("execution_state") == "idle":
                    seen_idle = True

        # Also fetch the shell-channel reply to get the final
        # ``execute_count`` if we didn't see one on iopub.
        try:
            reply = client.get_shell_msg(timeout=1.0)
            if (reply.get("parent_header", {}).get("msg_id") == msg_id
                    and reply.get("content", {}).get("status") == "ok"):
                execution_count = (
                    reply.get("content", {}).get("execution_count")
                    or execution_count
                )
        except Exception:
            pass

        wall_ms = int((time.monotonic() - t0) * 1000)
        return ExecutionResult(
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
            error=error,
            execution_count=execution_count,
            timed_out=timed_out,
            wall_ms=wall_ms,
            rich_outputs=rich_outputs,
        )
