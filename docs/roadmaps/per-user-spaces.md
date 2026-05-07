# Roadmap: Per-User Spaces (Workspace Path Virtualisation)

**Status:** Phase 1 starting on `dev`
**Last updated:** 2026-05-08

This document captures the design and phased implementation plan for the **Spaces** model — a per-user view of the global folder tree where each grant the user holds becomes an independently-rooted "space", and the system path structure (`/users/`, `/eng/`, etc.) never appears in the user's UI.

It is self-contained on purpose — readable without the prior design discussion — so context-window compression can't lose the key calls.

---

## TL;DR

The store keeps absolute global paths as the single source of truth for authz + retrieval scope. The **display layer** rewrites paths to be relative to the user's own grant roots, so:

* Each grant root the user holds (their personal folder, plus anything shared with them) becomes a top-level **space**.
* Inside a space, paths are relative — `/eng/secrets/q4-roadmap/spec.pdf` shows as `q4-roadmap/spec.pdf` for a user whose grant root is `/eng/secrets/q4-roadmap`.
* The user **never sees parent path segments** they don't have access to. No `/users/`, no `/eng/secrets/`, no leaking of sibling directory names through path strings.
* Backend storage, authz checks, retrieval scope, and the database schema are unchanged — only the API boundary translates.

This solves three problems simultaneously:
1. **Privacy**: shared paths leak organisational structure (folder names, hierarchy depth) via the path string today.
2. **Mental model**: users don't care about `/users/<their_username>/` as a system prefix; it's noise.
3. **Cross-user collaboration**: when admin shares `/eng/secrets/x` with bob, bob shouldn't be able to infer the existence of `/eng` or `/eng/secrets` from the path string.

---

## Architectural decisions (with rejected alternatives)

### 1. Spaces, not full path remap

**Chosen:** Each grant root becomes a top-level space; paths display as `(space_id, rel)` pairs. Same-name spaces (e.g. two grants both ending in `/q4`) get an owner-suffix disambiguator.

**Rejected: full path remap to a single virtual `/`.** Different grants might collide at root (every grant ends in some folder name; multiple grants could share that name), so a single namespace would need disambiguators on every entry, making the user's tree look like `/q4 (projects)`, `/q4 (eng)`, `/notes`, `/notes (alice)`. Spaces give a clean per-root namespace at zero collision cost.

**Rejected: soft visual flattening (keep absolute paths, just don't render unreachable parents).** The path string still contains the parent name, so a user with access to `/eng/secrets/x` can read `eng` and `secrets` from the breadcrumb / tooltip / search row. Defeats the privacy goal.

### 2. Display-only translation; storage unchanged

**Chosen:** `Folder.path` keeps absolute global paths. Authz still pattern-matches on path prefix. The `PathRemap` translator runs at the API boundary (input + output) for each request.

**Rejected: rewrite stored paths to be per-user.** Massive cascading writes when grants change. Re-share a folder → rewrite every descendant document's stored path. No good story for a folder listed in N users' spaces with N different "names".

### 3. Per-request resolver, not session-cached

**Chosen:** `PathRemap` is rebuilt on each request from the principal's current grant set. Cheap (one query for the user's grant roots) and always fresh.

**Rejected: cached per-session.** Grants can change mid-session (admin grants/revokes); caching would surface stale spaces until session refresh.

### 4. Conflict disambiguator: owner suffix

**Chosen:** Two grants whose basenames collide → append `(owner)` to the second one onward. Stable + readable. Admin-shared folders show `q4-spec (alice)`.

**Rejected: numeric suffix `(2)`.** Order-dependent, inconsistent across users.

**Rejected: full path in parentheses.** Long, cluttered, and re-introduces the parent-name leak we're trying to avoid.

### 5. Phased rollout

**Chosen:** Backend remap layer + frontend tree first (Phase 1, high-traffic surface). Then doc detail / search / chat scope picker (Phase 2). Then citations / KG (Phase 3). Each phase ships independently.

**Rejected: big-bang rewrite.** Too much surface area to test atomically. Hard to bisect regressions.

---

## Data model & key types

```python
# api/auth/path_remap.py (new)

@dataclass
class Space:
    space_id: str          # opaque; usually the basename + disambiguator
    name: str              # display name, e.g. "q4-spec (alice)"
    abs_root: str          # absolute path of the grant root, e.g. "/eng/q4-spec"
    role: str              # "rw" / "r" — user's role on the root
    is_personal: bool      # True for the user's own /users/<username>

class PathRemap:
    """Per-request translator. Stateless once built — methods just look up
    in self._spaces / self._abs_to_space."""

    def __init__(self, spaces: list[Space]): ...

    @property
    def spaces(self) -> list[Space]: ...

    def to_user(self, abs_path: str) -> tuple[str, str] | None:
        """``/eng/q4-spec/notes/x.md`` → ``("q4-spec", "notes/x.md")``.
        Returns None if abs_path isn't reachable to this user."""

    def to_abs(self, space_id: str, rel_path: str) -> str:
        """Inverse. Raises if space_id is unknown to this user."""
```

```python
# api/auth/authz.py — add helper

def user_grant_roots(self, user_id: str) -> list[GrantRoot]:
    """Topmost paths user has direct access to via shared_with.
    Excludes nested grants (if user has access to /a and /a/b, only
    /a is returned — /a/b is reachable through /a)."""
```

API response shape change for path-bearing endpoints:

```jsonc
// Before
{ "path": "/eng/secrets/q4-spec/notes/x.md" }

// After
{
  "space": { "id": "q4-spec", "name": "q4-spec (alice)" },
  "rel_path": "notes/x.md"
}
```

For input (e.g. chat scope), accept either shape and convert. Keep the absolute-path input working for back-compat (admin / API user with full access).

---

## Phase plan

### Phase 1 — backend remap + workspace tree

**Goal:** ship a working "spaces" view of the workspace tree.

* `api/auth/path_remap.py`: `Space` + `PathRemap` + `user_grant_roots`.
* `api/state.py` or a request-scoped dep: builds `PathRemap` once per request from `principal.user_id`.
* `api/routes/folders.py`: `/folders/tree` returns `{ spaces: [{ space, rel_tree: [...] }] }` instead of a single global tree. Inputs (create folder under a path, etc.) accept `(space_id, rel)` and convert. Old absolute-path inputs still accepted (admin tooling).
* `web/src/views/Workspace.vue` + `FolderTree.vue`: render multiple top-level space cards. Each space's tree is rooted at its own `rel_path = ""`.
* No changes to doc detail, search, citations, chat scope yet — those keep using absolute paths internally; users just see them in the doc detail breadcrumb / search row. Acceptable for one phase.

**Done when:** a user with two grants (their personal folder + one shared folder) sees two top-level spaces in `/workspace`. Folder create / rename / move within a space works. Cross-space move is rejected (or routes through admin).

### Phase 2 — doc detail, search, chat scope picker

* Doc detail: breadcrumb shows space + rel path. URL params (`?doc=...`) keep using doc_id (no path involved).
* Search: each result row shows space badge + rel path.
* Chat scope picker: top-level options are spaces; drill into a space to pick a sub-folder. Selected scope sent to `/agent/chat` as `(space_id, rel)`; backend translates to absolute for `path_filters`.
* Workspace search bar (if added): scoped to current space by default.

### Phase 3 — citations, KG, polish

* Citation rail: file path under space-relative form.
* Knowledge graph: node tooltips use space-relative paths.
* Disambiguator UX polish: when two spaces collide on basename, owner-suffix kicks in. Add unit tests covering 2-grant-collision and 3-grant-collision.
* Migrate any remaining absolute-path responses to space-relative.

---

## Edge cases

* **Nested grants.** User has grant on `/a` AND `/a/b`. The latter is redundant (already reachable via `/a`). `user_grant_roots` returns only the topmost. The `/a/b` row in `shared_with` stays in DB (it's the explicit grant) but doesn't surface as a separate space.
* **Zero grants.** New user with no shared_with anywhere → empty workspace, empty state with "ask an admin to share a folder" prompt. (Also why we still want to auto-create `/users/<username>` at registration.)
* **Cascading grant changes.** Admin grants / revokes mid-session → next request rebuilds `PathRemap` → space list updates. Tree component refetches naturally on `/workspace` mount; if the user is mid-action when a grant is revoked, the next API call returns 403 and the UI bounces.
* **Cross-space move.** Phase 1 rejects. Phase 4 (later) could allow it if the user has rw on both spaces; backend converts both endpoints to absolute and runs the existing folder move.
* **Trash.** `/__trash__` is a system folder admin-only. Users see a per-space trash view (Phase 3) or a dedicated "trash" entry next to spaces.

---

## Open questions (revisit before Phase 2)

1. Should the personal space (`/users/<me>`) get a special name like "Home" or just show as `<username>`? Current lean: show as `<username>`, let the user mentally treat their own folder as their identity.
2. When user clicks into a sub-folder of a shared space, should the URL bar reflect `(space_id, rel)` or doc_id only? Lean: keep URLs opaque (doc_id), so links shared with another user don't depend on that user having the same space layout.
3. Search results from across multiple spaces — group by space in the UI, or interleave? Lean: interleave by score, badge each row with its space.
