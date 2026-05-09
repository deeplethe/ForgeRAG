"""
SandboxManager — per-user agent container lifecycle.

The Phase 2 architectural decision (see docs/roadmaps/agent-workspace.md):

    one Docker container per user
    one ipykernel per project (subprocess inside the user's container)

This module owns the OUTER layer — starting / stopping / reaping
containers. The kernel-orchestration layer (jupyter_client +
``docker exec`` to launch ipykernels) lands in Phase 2.3 on top
of the same handles this manager hands out.

Design points:

* **Backend abstraction**. ``SandboxBackend`` is a Protocol with
  ``start_container``, ``exec``, ``stop``, ``is_running``,
  ``list_owned``. The default is ``DockerBackend`` (docker SDK).
  Future hardened deployments can swap in ``DockerSandboxesBackend``
  (microVM) or ``K8sBackend`` without touching SandboxManager.
  The roadmap calls this out explicitly: "If a customer eventually
  asks for hardened isolation, our SandboxManager exposes a
  pluggable backend interface — that's the integration point."

* **In-memory handle table**. Live containers tracked in
  ``self._containers: dict[user_id, ContainerHandle]``. Per-user
  ``threading.Lock`` so two concurrent ``ensure_container`` calls
  don't race to start two containers for the same user.

* **Idle reaping** is opt-in, called explicitly by a maintenance
  loop (Phase 2.9 wires the periodic call into AppState; for now
  callers invoke ``reap_idle()`` directly). Container idle = no
  ``touch()`` call within ``container_idle_seconds``. Default 30 min.

* **Restart recovery** (Phase 2.9). A FastAPI worker restart loses
  the in-memory table. On next ``ensure_container_for_user``, the
  backend's ``list_owned`` reports any container the previous
  process left running with our naming convention; we adopt it
  rather than spawning a duplicate.

* **No DB writes here**. ExecutionSession DB persistence lands in
  Phase 2.9. Keeping persistence out of the manager's hot path
  makes every operation cheap and side-effect-free for the test
  suite (fake backend + in-memory handles is enough).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Protocol, runtime_checkable

log = logging.getLogger(__name__)


# Container naming convention — `<prefix><user_id>`. Lets us list
# adopted containers across worker restart by name pattern alone,
# no DB lookup required (DB-recorded ExecutionSession rows in
# Phase 2.9 will cross-check, but the in-memory recovery path
# survives a fresh process even before that lands).
DEFAULT_CONTAINER_NAME_PREFIX = "opencraig-sandbox-"

# Default image tag built by ``scripts/build-sandbox.sh``. Operators
# pin a specific tag in their config; the default points at the
# current Phase-2 image so a fresh checkout works without yaml
# edits.
DEFAULT_SANDBOX_IMAGE = "opencraig/sandbox:py3.13"

# Idle thresholds (matched to roadmap "Container lifecycle" section).
# Kernel-level idle reaping is Phase 2.3's responsibility; container
# idle is owned here.
DEFAULT_CONTAINER_IDLE_SECONDS = 30 * 60   # 30 min — see roadmap rationale
DEFAULT_KERNEL_IDLE_SECONDS = 10 * 60      # exposed for completeness


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mount:
    """A bind-mount specification handed to the backend.

    Always read-write — the agent needs to write outputs into project
    workdirs. ``host_path`` MUST exist before container start (the
    backend doesn't auto-create); SandboxManager is responsible for
    pre-creating per-user volumes via the AgentConfig defaults.
    """

    host_path: str
    container_path: str

    def __post_init__(self) -> None:
        # Best-effort sanity — backends will re-validate but a clear
        # error here saves a confusing docker daemon error later.
        # Container paths are ALWAYS POSIX (the container is Linux
        # regardless of where the host runs); using PurePosixPath
        # avoids host-OS skew (Path("/foo").is_absolute() returns
        # False on Windows because there's no drive letter).
        if not self.host_path or not self.container_path:
            raise ValueError("Mount needs both host_path and container_path")
        if not PurePosixPath(self.container_path).is_absolute():
            raise ValueError(
                f"container_path must be absolute (POSIX): "
                f"{self.container_path!r}"
            )


@dataclass
class ExecResult:
    """Result of a one-shot ``exec`` against a running container.

    Phase 2.3+ uses the dedicated kernel channel for streaming
    execution; ``exec`` is for kernel-launch + setup-style commands
    (``micromamba install ...``, ``cat /workspace/.envs/r/.ready``,
    etc.) where one-shot stdout/stderr capture is enough.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes


@dataclass
class ContainerHandle:
    """Liveness handle for a user's sandbox container.

    Mutable: ``last_active_at`` ticks on every ``exec`` call routed
    through SandboxManager so idle reaping doesn't kill an
    actively-used container. Construction is exclusively
    SandboxManager's job; callers receive these via
    ``ensure_container_for_user``.
    """

    user_id: str
    container_id: str
    image: str
    name: str
    mounts: tuple[Mount, ...]
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_active_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Backend Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SandboxBackend(Protocol):
    """Pluggable container backend.

    The default ``DockerBackend`` wraps the docker Python SDK.
    Alternative implementations (BoxLite microVMs, Docker Sandboxes
    `sbx`, k8s pods, ...) just need to honor this contract:

    * ``start_container`` returns an opaque container_id and blocks
      until the container is ready to accept ``exec``
    * ``exec`` runs a one-shot command, captures stdout+stderr+rc
    * ``stop`` removes the container; idempotent
    * ``is_running`` is cheap (must be safe to call in tight reap
      loops)
    * ``list_owned`` returns container_ids the backend believes
      belong to us — used by SandboxManager to adopt orphans after
      a worker restart
    """

    def start_container(
        self,
        *,
        image: str,
        name: str,
        mounts: Iterable[Mount],
        env: dict[str, str] | None = None,
        published_ports: dict[int, int] | None = None,
    ) -> str:
        ...

    def exec(
        self,
        container_id: str,
        cmd: list[str],
        *,
        timeout: float | None = None,
        workdir: str | None = None,
        detach: bool = False,
    ) -> ExecResult:
        ...

    def stop(self, container_id: str, *, timeout: int = 10) -> None:
        ...

    def is_running(self, container_id: str) -> bool:
        ...

    def list_owned(self, *, name_prefix: str) -> list[tuple[str, str]]:
        """Return ``(name, container_id)`` for each container whose
        name starts with ``name_prefix`` (left over from a previous
        process). Adopted on next ``ensure_container_for_user``."""
        ...


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SandboxStartError(RuntimeError):
    """Raised when a container fails to start AFTER backend retries."""


class SandboxNotFoundError(RuntimeError):
    pass


class SandboxManager:
    """Per-user container lifecycle. Process-local state.

    Usage:

        manager = SandboxManager(
            backend=DockerBackend(client),
            image="opencraig/sandbox:py3.13",
            projects_root=Path("storage/projects"),
            user_envs_root=Path("storage/user-envs"),
        )
        handle = manager.ensure_container_for_user(
            user_id="u_alice",
            owned_project_ids=["proj_001", "proj_002"],
        )
        # handle.container_id is now alive; Phase 2.3 launches
        # ipykernels via docker exec on it.
    """

    def __init__(
        self,
        *,
        backend: SandboxBackend,
        image: str = DEFAULT_SANDBOX_IMAGE,
        projects_root: Path | str = "storage/projects",
        user_envs_root: Path | str = "storage/user-envs",
        user_workdirs_root: Path | str | None = None,
        container_name_prefix: str = DEFAULT_CONTAINER_NAME_PREFIX,
        container_idle_seconds: int = DEFAULT_CONTAINER_IDLE_SECONDS,
        published_ports: dict[int, int] | None = None,
        clock: callable = datetime.utcnow,  # injectable for tests
    ):
        self.backend = backend
        self.image = image
        self.projects_root = Path(projects_root)
        self.user_envs_root = Path(user_envs_root)
        # Per-user workdir tree (folder-as-cwd). Each user gets a
        # private filesystem at ``<user_workdirs_root>/<user_id>/``
        # that's bind-mounted to ``/workdir/`` in their container.
        # Chat ``cwd_path`` is interpreted RELATIVE to this mount
        # — UI shows ``/sales/2025/``, host has
        # ``<root>/<user_id>/sales/2025/``, container sees
        # ``/workdir/sales/2025/``.
        #
        # ``None`` keeps the legacy per-project mount behaviour (for
        # the rare deployment that hasn't migrated to folder-as-cwd
        # yet, or for tests that don't want the user-workdir
        # auto-create).
        self.user_workdirs_root = (
            Path(user_workdirs_root) if user_workdirs_root else None
        )
        self.container_name_prefix = container_name_prefix
        self.container_idle_seconds = container_idle_seconds
        # Port range to publish on every fresh container. Originally
        # allocated for ipykernel ZMQ; under the Claude Agent SDK model
        # the in-container agent doesn't need bound ports, but we
        # keep the range for any future MCP / debug / preview server
        # that wants to listen. All bindings stay loopback-only at
        # the docker layer; no exposure to non-localhost callers.
        self.published_ports = published_ports
        self._clock = clock

        # user_id → ContainerHandle. Module-level table; one
        # SandboxManager per process is the assumption (plumbed via
        # AppState in Phase 2.9).
        self._containers: dict[str, ContainerHandle] = {}
        # Per-user start lock so concurrent ensure_container calls
        # for the SAME user serialise. Dict-of-locks; we never
        # delete entries (cheap; bounded by user count).
        self._user_locks: dict[str, threading.Lock] = {}
        self._table_lock = threading.Lock()

        # Adoption flag: on first ensure_container call we sweep
        # the backend for orphans. One-shot per process.
        self._adopted = False

    # ── Public API ────────────────────────────────────────────────

    def ensure_container_for_user(
        self,
        user_id: str,
        *,
        owned_project_ids: Iterable[str] = (),
        extra_env: dict[str, str] | None = None,
    ) -> ContainerHandle:
        """Return a live container for ``user_id``, starting one if
        none is alive.

        ``owned_project_ids`` controls which project workdirs get
        bind-mounted into ``/workdir/<project_id>``. New projects
        created during a session won't be visible until the next
        container restart — that's a deliberate trade-off; live
        mount-add isn't supported by docker and reshaping the user
        view of /workdir would invalidate any in-flight kernel state
        for OTHER projects. Phase 2.9 may add explicit
        "restart-with-new-projects" once usage data shows it.
        """
        self._adopt_orphans_once()

        lock = self._lock_for(user_id)
        with lock:
            existing = self._containers.get(user_id)
            if existing is not None and self.backend.is_running(existing.container_id):
                self._touch(user_id)
                return existing
            # Stale entry — backend says container's gone. Drop it
            # and fall through to a fresh start.
            if existing is not None:
                log.info(
                    "sandbox: dropping stale handle for user=%s container=%s",
                    user_id,
                    existing.container_id,
                )
                self._containers.pop(user_id, None)

            handle = self._start_for_user(
                user_id,
                owned_project_ids=tuple(owned_project_ids),
                extra_env=extra_env,
            )
            self._containers[user_id] = handle
            return handle

    def get(self, user_id: str) -> ContainerHandle | None:
        """Return the live handle for ``user_id`` if any. Cheap; no
        backend round-trip."""
        return self._containers.get(user_id)

    def touch(self, user_id: str) -> None:
        """Mark ``user_id``'s container as recently used. Called by
        the kernel orchestration layer (Phase 2.3) after every
        ``execute_request`` so reaping doesn't murder an active
        kernel."""
        with self._table_lock:
            self._touch(user_id)

    def stop_user(self, user_id: str) -> bool:
        """Explicitly stop ``user_id``'s container. Returns True if
        a container was stopped, False if there was nothing to do.
        Used by admin / shutdown paths."""
        lock = self._lock_for(user_id)
        with lock:
            handle = self._containers.pop(user_id, None)
            if handle is None:
                return False
            try:
                self.backend.stop(handle.container_id)
            except Exception:
                # Don't pollute the manager's state with a half-
                # stopped container — we already evicted the handle.
                # The next ensure_container will start a fresh one.
                log.exception(
                    "sandbox: backend.stop failed user=%s container=%s — "
                    "treating as gone",
                    user_id,
                    handle.container_id,
                )
            return True

    def reap_idle(self) -> int:
        """Stop containers that have been idle past
        ``container_idle_seconds``. Returns the count reaped."""
        cutoff = self._clock() - timedelta(seconds=self.container_idle_seconds)
        # Snapshot under the table lock to avoid mutating dict
        # while iterating; per-user locks acquired one at a time
        # outside the table lock to keep contention down.
        with self._table_lock:
            candidates = [
                (uid, h)
                for uid, h in self._containers.items()
                if h.last_active_at < cutoff
            ]
        n = 0
        for uid, handle in candidates:
            lock = self._lock_for(uid)
            with lock:
                # Re-check under the lock — a concurrent touch could
                # have refreshed the timestamp between snapshot + acquire.
                cur = self._containers.get(uid)
                if cur is None or cur.container_id != handle.container_id:
                    continue
                if cur.last_active_at >= cutoff:
                    continue
                log.info(
                    "sandbox: reaping idle container user=%s id=%s "
                    "last_active=%s",
                    uid,
                    cur.container_id,
                    cur.last_active_at.isoformat(),
                )
                try:
                    self.backend.stop(cur.container_id)
                except Exception:
                    log.exception(
                        "sandbox: backend.stop during reap failed user=%s",
                        uid,
                    )
                self._containers.pop(uid, None)
                n += 1
        return n

    def shutdown(self) -> None:
        """Stop every live container — called from AppState.shutdown."""
        with self._table_lock:
            handles = list(self._containers.items())
            self._containers.clear()
        for uid, handle in handles:
            try:
                self.backend.stop(handle.container_id)
            except Exception:
                log.exception(
                    "sandbox: shutdown stop failed user=%s container=%s",
                    uid,
                    handle.container_id,
                )

    def list_live(self) -> list[ContainerHandle]:
        """Snapshot of currently-live handles. Used by the admin
        sandbox-monitor page (Phase 2.9 / observability)."""
        with self._table_lock:
            return list(self._containers.values())

    # ── Internals ────────────────────────────────────────────────

    def _lock_for(self, user_id: str) -> threading.Lock:
        # CPython's ``dict.setdefault`` is atomic for the simple
        # "get or create" pattern, so we don't need to acquire
        # ``_table_lock`` here. Doing so would serialize all users
        # through this method (every ``ensure_container_for_user``
        # passes through it) — a real bottleneck under multi-user
        # load that the audit (May 2026) flagged. Audit finding #4.
        return self._user_locks.setdefault(user_id, threading.Lock())

    def _touch(self, user_id: str) -> None:
        h = self._containers.get(user_id)
        if h is not None:
            h.last_active_at = self._clock()

    def _container_name(self, user_id: str) -> str:
        # Container names must be [a-zA-Z0-9_.-]+ for docker. user_id
        # is already a hex token in our schema so this is safe — but
        # we still sanity-strip in case a future user_id format
        # introduces unsafe chars.
        safe = "".join(
            c if c.isalnum() or c in "._-" else "_" for c in user_id
        )
        return f"{self.container_name_prefix}{safe}"

    def _build_mounts(
        self,
        user_id: str,
        owned_project_ids: tuple[str, ...],
    ) -> tuple[Mount, ...]:
        """Compose the bind-mount list for a fresh container.

        Two layouts coexist while we transition out of the legacy
        Project-bound model:

            user_workdirs_root SET (folder-as-cwd, the v0.5.0 OSS
            path):
                <user_workdirs_root>/<user_id>  → /workdir
                <user_envs_root>/<user_id>      → /workspace/.envs

            user_workdirs_root NOT SET (legacy per-project, kept for
            deployments still on the project-bound model):
                <projects_root>/<pid>           → /workdir/<pid>     (per owned project)
                <user_envs_root>/<user_id>      → /workspace/.envs

        The folder-as-cwd path is the future; ClaudeContainerRunner
        always passes ``owned_project_ids=()`` so the per-project
        branch is dead code under the new chat route.
        """
        mounts: list[Mount] = []

        if self.user_workdirs_root is not None:
            # Folder-as-cwd: ONE mount, the user's private workdir
            # tree. Auto-create the host dir on first ensure so the
            # first chat doesn't fail with "host path missing" — same
            # affordance the user_envs mount below uses.
            user_workdir = self.user_workdirs_root / user_id
            user_workdir.mkdir(parents=True, exist_ok=True)
            mounts.append(
                Mount(
                    host_path=str(user_workdir.resolve()),
                    container_path="/workdir",
                )
            )
        else:
            # Legacy per-project mounts. Skip silently for missing
            # host dirs — ProjectService.create scaffolds the dir
            # before any agent work, so a missing dir means a stale
            # project_id sneaked through; we'd rather start the
            # container with one fewer mount than fail outright.
            for pid in owned_project_ids:
                host = self.projects_root / pid
                if not host.exists():
                    log.warning(
                        "sandbox: project workdir missing user=%s project=%s "
                        "expected=%s — skipping mount",
                        user_id,
                        pid,
                        host,
                    )
                    continue
                mounts.append(
                    Mount(
                        host_path=str(host.resolve()),
                        container_path=f"/workdir/{pid}",
                    )
                )

        # Per-user envs volume. Auto-created if missing; this is the
        # single source of truth for install_runtime caches and
        # outlives container restarts.
        user_env = self.user_envs_root / user_id
        user_env.mkdir(parents=True, exist_ok=True)
        mounts.append(
            Mount(
                host_path=str(user_env.resolve()),
                container_path="/workspace/.envs",
            )
        )
        return tuple(mounts)

    def _start_for_user(
        self,
        user_id: str,
        *,
        owned_project_ids: tuple[str, ...],
        extra_env: dict[str, str] | None,
    ) -> ContainerHandle:
        name = self._container_name(user_id)
        mounts = self._build_mounts(user_id, owned_project_ids)
        env = dict(extra_env or {})
        log.info(
            "sandbox: starting container user=%s name=%s image=%s "
            "mounts=%d",
            user_id,
            name,
            self.image,
            len(mounts),
        )
        try:
            cid = self.backend.start_container(
                image=self.image,
                name=name,
                mounts=mounts,
                env=env or None,
                published_ports=self.published_ports,
            )
        except Exception as e:
            raise SandboxStartError(
                f"failed to start sandbox for user={user_id!r}: {e}"
            ) from e
        now = self._clock()
        return ContainerHandle(
            user_id=user_id,
            container_id=cid,
            image=self.image,
            name=name,
            mounts=mounts,
            started_at=now,
            last_active_at=now,
        )

    def _adopt_orphans_once(self) -> None:
        """First-call sweep: pick up containers from a previous
        process that match our naming convention. Avoids spawning
        a duplicate after a FastAPI worker restart.

        Best-effort. If the backend lookup fails, we fall through
        to fresh starts; orphan containers will trip a name-collision
        on the next start_container and the backend reports it
        cleanly.
        """
        if self._adopted:
            return
        with self._table_lock:
            if self._adopted:
                return
            self._adopted = True
        try:
            owned = self.backend.list_owned(name_prefix=self.container_name_prefix)
        except Exception:
            log.exception("sandbox: orphan-adoption sweep failed; ignoring")
            return
        for name, cid in owned:
            user_id = name[len(self.container_name_prefix):]
            if not user_id:
                continue
            log.info(
                "sandbox: adopted orphan container user=%s name=%s id=%s",
                user_id,
                name,
                cid,
            )
            now = self._clock()
            self._containers[user_id] = ContainerHandle(
                user_id=user_id,
                container_id=cid,
                image=self.image,
                name=name,
                mounts=(),  # we don't know what they were; restart respawns clean
                started_at=now,
                last_active_at=now,
                metadata={"adopted": True},
            )


# ---------------------------------------------------------------------------
# Default backend — docker SDK
# ---------------------------------------------------------------------------


class DockerBackend:
    """Default ``SandboxBackend`` using the ``docker`` Python SDK.

    Lazy import of the docker package — keeps the module importable
    on machines without docker installed (tests run with a fake
    backend; production sets up DockerBackend explicitly).

    No retry loops here yet — the docker daemon's own error surface
    (`APIError`, `ImageNotFound`, etc.) is the right abstraction for
    callers to catch. SandboxManager wraps in SandboxStartError for
    a clean caller contract.
    """

    def __init__(self, client: Any = None):
        # ``client`` injectable for tests and for advanced operators
        # who pre-configure the docker.DockerClient (e.g. with a
        # remote daemon URL).
        self._client = client

    def _docker(self) -> Any:
        if self._client is None:
            try:
                import docker  # type: ignore[import-not-found]
            except ImportError as e:
                raise RuntimeError(
                    "docker SDK not installed. pip install docker. "
                    "(The agent sandbox needs a Docker daemon at runtime; "
                    "install the SDK so SandboxManager can talk to it.)"
                ) from e
            self._client = docker.from_env()
        return self._client

    def start_container(
        self,
        *,
        image: str,
        name: str,
        mounts: Iterable[Mount],
        env: dict[str, str] | None = None,
        published_ports: dict[int, int] | None = None,
    ) -> str:
        client = self._docker()
        # Mounts dict shape required by the SDK
        binds = {
            m.host_path: {"bind": m.container_path, "mode": "rw"}
            for m in mounts
        }
        # Port-publish dict for the SDK: container_port → host_port.
        # ALL bindings forced to 127.0.0.1 so the kernel ZMQ ports
        # are reachable from the host process but never exposed to
        # the LAN. Without the loopback bind, ``-p 5555:5555`` would
        # listen on 0.0.0.0 by default — that's a multi-tenant
        # security hole even on a dev box.
        port_spec: dict[str, tuple[str, int] | None] = {}
        if published_ports:
            for container_port, host_port in published_ports.items():
                port_spec[f"{container_port}/tcp"] = ("127.0.0.1", host_port)
        # If a previous run left a container with the same name, the
        # SDK raises 409. Adoption sweep should have caught this; if
        # it didn't, the orphan is genuinely orphaned and we'd
        # rather refuse than silently merge two states.
        container = client.containers.run(
            image=image,
            name=name,
            detach=True,
            volumes=binds,
            environment=env or {},
            # Bridge network so the published ports actually route
            # via docker's NAT table; the kernel TCP listeners are
            # the only thing reachable, and only via 127.0.0.1.
            network_mode="bridge",
            ports=port_spec or None,
            # restart=no means a kernel crash inside doesn't bounce
            # the whole container; SandboxManager owns the lifecycle
            # decision.
            restart_policy={"Name": "no"},
            # Don't auto-remove — explicit stop() lets us drain
            # in-flight exec calls cleanly.
            auto_remove=False,
            tty=False,
            stdin_open=False,
        )
        return container.id

    def exec(
        self,
        container_id: str,
        cmd: list[str],
        *,
        timeout: float | None = None,
        workdir: str | None = None,
        detach: bool = False,
    ) -> ExecResult:
        client = self._docker()
        container = client.containers.get(container_id)
        # ``detach=True`` is for launching long-running processes
        # in the container (e.g. an MCP-bridged agent runtime). If
        # we waited synchronously the call would block forever.
        # Detached: ``exec_run`` returns immediately and the process
        # keeps running in the background. We get no exit code back;
        # the caller verifies liveness through whatever protocol the
        # detached process speaks.
        if detach:
            container.exec_run(
                cmd,
                workdir=workdir,
                detach=True,
                tty=False,
                stdin=False,
            )
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        # docker SDK's exec_run returns (exit_code, output) where
        # output is a single bytes blob with stderr merged in unless
        # we ask for them separately. Demux is what we want for
        # the agent (errors visible to the LLM separately from
        # stdout it can quote back).
        rc, out = container.exec_run(
            cmd,
            workdir=workdir,
            demux=True,
            tty=False,
            stdin=False,
        )
        # demux=True returns (stdout, stderr); each may be None when
        # empty. Normalise to bytes.
        stdout, stderr = (out or (None, None))
        return ExecResult(
            exit_code=int(rc),
            stdout=stdout or b"",
            stderr=stderr or b"",
        )

    def stop(self, container_id: str, *, timeout: int = 10) -> None:
        client = self._docker()
        try:
            c = client.containers.get(container_id)
        except Exception:
            # Already gone — idempotent contract
            return
        try:
            c.stop(timeout=timeout)
        except Exception:
            log.exception("docker stop failed id=%s; trying remove", container_id)
        try:
            c.remove(force=True)
        except Exception:
            log.exception("docker remove failed id=%s", container_id)

    def is_running(self, container_id: str) -> bool:
        client = self._docker()
        try:
            c = client.containers.get(container_id)
        except Exception:
            return False
        return getattr(c, "status", "") == "running"

    def list_owned(self, *, name_prefix: str) -> list[tuple[str, str]]:
        client = self._docker()
        out: list[tuple[str, str]] = []
        try:
            for c in client.containers.list(all=False):
                # ``c.name`` may include a leading slash on some
                # docker SDK versions — strip it for the prefix
                # match.
                name = (getattr(c, "name", "") or "").lstrip("/")
                if name.startswith(name_prefix):
                    out.append((name, c.id))
        except Exception:
            log.exception("docker list_containers failed during orphan sweep")
        return out
