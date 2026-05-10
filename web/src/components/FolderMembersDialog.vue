<!--
  FolderMembersDialog — modal for managing who has access to a folder.

  Visible to anyone with read access to the folder (so members can
  see who else is in); the add / remove / role-change actions are
  gated by the backend's ``share`` permission (folder owner or
  admin only). The UI mirrors that — for non-owners, the controls
  render but the API will 403, and we surface the error inline.

  Inherited entries (cascaded from a parent folder) are rendered
  with a small "from /parent" tag and have their controls disabled.
  Editing has to happen on the ancestor — server enforces this with
  a 409 we map to a friendly inline message.

  Usage:
    <FolderMembersDialog
      v-model:open="membersOpen"
      :folder-id="folder.folder_id"
      :folder-label="folder.name"
    />
-->
<template>
  <Teleport to="body">
    <Transition name="dialog">
      <div
        v-if="open"
        class="dialog-backdrop"
        @click.self="onClose"
        @keydown.esc="onClose"
      >
        <div class="panel" role="dialog" aria-modal="true" tabindex="-1" ref="dialogEl">

          <header class="header">
            <div>
              <div class="title">Members</div>
              <div class="subtitle">
                Who can see <span class="folder-label">{{ folderLabel || '/' }}</span>
              </div>
            </div>
            <button class="close-btn" @click="onClose" aria-label="Close">
              <X :size="16" :stroke-width="1.75" />
            </button>
          </header>

          <!-- ── Add row ─────────────────────────────────────── -->
          <div class="add-row" v-if="canManage !== false">
            <div class="search-wrap">
              <Search :size="14" :stroke-width="1.75" class="search-icon" />
              <input
                v-model="query"
                ref="searchInput"
                class="search-input"
                placeholder="Search by email, name, or username"
                @input="onQueryInput"
                @keydown.down.prevent="moveSuggestion(1)"
                @keydown.up.prevent="moveSuggestion(-1)"
                @keydown.enter.prevent="onPickSuggestion(suggestions[suggestionIdx])"
                @keydown.esc="suggestions = []"
              />
              <span v-if="searchLoading" class="search-spin"><Spinner :size="12" /></span>
            </div>

            <select v-model="newRole" class="role-select">
              <option value="r">Can view</option>
              <option value="rw">Can edit</option>
            </select>

            <button
              class="btn-primary"
              :disabled="!pickedUser || addLoading"
              @click="onAdd"
            >{{ addLoading ? 'Adding…' : 'Add' }}</button>

            <!-- Suggestions popover (anchored under the search input) -->
            <div v-if="suggestions.length" class="suggestions">
              <button
                v-for="(s, i) in suggestions"
                :key="s.user_id"
                type="button"
                class="suggestion"
                :class="{ 'is-active': i === suggestionIdx, 'is-disabled': isAlreadyMember(s) }"
                :disabled="isAlreadyMember(s)"
                @mouseenter="suggestionIdx = i"
                @click="onPickSuggestion(s)"
              >
                <UserAvatar :name="s.display_name || s.email || s.username" :img-url="avatarUrlFor(s.user_id, s.has_avatar)" :size="22" />
                <span class="suggestion-meta">
                  <span class="suggestion-name">{{ s.display_name || s.username }}</span>
                  <span class="suggestion-email">{{ s.email || s.username }}</span>
                </span>
                <span v-if="isAlreadyMember(s)" class="badge-already">already a member</span>
              </button>
            </div>
          </div>

          <!-- ── Picked user preview (chip-style, before clicking Add) ─── -->
          <div v-if="pickedUser" class="picked-row">
            <UserAvatar
              :name="pickedUser.display_name || pickedUser.email || pickedUser.username"
              :img-url="avatarUrlFor(pickedUser.user_id, pickedUser.has_avatar)"
              :size="20"
            />
            <span class="picked-name">{{ pickedUser.display_name || pickedUser.username }}</span>
            <span class="picked-email">{{ pickedUser.email }}</span>
            <button class="picked-clear" @click="clearPicked" aria-label="Clear">
              <X :size="12" :stroke-width="1.75" />
            </button>
          </div>

          <!-- ── Member list ─────────────────────────────────── -->
          <div class="members">
            <div v-if="loading" class="empty">Loading…</div>
            <div v-else-if="!members.length" class="empty">No members yet.</div>

            <div
              v-for="m in members"
              :key="m.user_id"
              class="member-row"
              :class="{ 'is-self': isMe(m) }"
            >
              <UserAvatar
                :name="m.display_name || m.email || m.username"
                :img-url="avatarUrlFor(m.user_id, m.has_avatar)"
                :size="28"
              />
              <div class="member-meta">
                <div class="member-name-row">
                  <span class="member-name">{{ m.display_name || m.username }}</span>
                  <span v-if="isMe(m)" class="badge-you">You</span>
                  <span v-if="m.role === 'owner'" class="badge-owner">Owner</span>
                  <span v-else-if="m.source && m.source.startsWith('inherited:')"
                        class="badge-inherited"
                        :title="`Inherited from ${m.source.slice('inherited:'.length)}`">
                    Inherited
                  </span>
                </div>
                <div class="member-email">{{ m.email || m.username }}</div>
              </div>

              <select
                v-if="m.role !== 'owner' && !isInherited(m)"
                :value="m.role"
                class="role-select role-select--row"
                :disabled="busyId === m.user_id"
                @change="onChangeRole(m, $event.target.value)"
              >
                <option value="r">View</option>
                <option value="rw">Edit</option>
              </select>
              <span v-else class="role-static">{{ roleLabel(m.role) }}</span>

              <button
                v-if="m.role !== 'owner' && !isInherited(m) && !isMe(m)"
                class="btn-remove"
                :disabled="busyId === m.user_id"
                :title="`Remove ${m.display_name || m.username}`"
                @click="onRemove(m)"
              >
                <Trash2 :size="13" :stroke-width="1.75" />
              </button>
            </div>
          </div>

          <!-- Inline error banner -->
          <div v-if="error" class="error">{{ error }}</div>

          <footer class="footer">
            <button class="btn-secondary" @click="onClose">Done</button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { Search, Trash2, X } from 'lucide-vue-next'
import {
  listFolderMembers,
  addFolderMember,
  updateFolderMemberRole,
  removeFolderMember,
} from '@/api/folders'
import { searchUsers, getMe } from '@/api/auth'
import { avatarUrlFor } from '@/api/admin'
import UserAvatar from '@/components/UserAvatar.vue'
import Spinner from '@/components/Spinner.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  folderId: { type: String, default: null },
  folderLabel: { type: String, default: '' },
})
const emit = defineEmits(['update:open'])

// ── State ────────────────────────────────────────────────────────
const members = ref([])
const loading = ref(false)
const busyId = ref(null)         // user_id currently being mutated
const error = ref('')
const me = ref(null)
// ``canManage`` is true when the user is owner/admin. We don't gate
// rendering on this — the backend is the authority — but we hide
// the Add row when we're sure they can't, to reduce confusion.
// null = unknown (don't hide).
const canManage = ref(null)

const query = ref('')
const suggestions = ref([])
const suggestionIdx = ref(0)
const searchLoading = ref(false)
const pickedUser = ref(null)
const newRole = ref('r')
const addLoading = ref(false)

const searchInput = ref(null)
const dialogEl = ref(null)

// ── Open/close lifecycle ────────────────────────────────────────
watch(() => props.open, async (v) => {
  if (v) {
    error.value = ''
    members.value = []
    pickedUser.value = null
    query.value = ''
    suggestions.value = []
    newRole.value = 'r'
    await Promise.all([loadMembers(), loadMe()])
    await nextTick()
    searchInput.value?.focus()
  }
})

function onClose() {
  emit('update:open', false)
}

// ── Member list ─────────────────────────────────────────────────
async function loadMembers() {
  if (!props.folderId) return
  loading.value = true
  try {
    members.value = await listFolderMembers(props.folderId)
    // Detect "can manage": if the current user (when known) is
    // owner OR admin (admin role surfaces via /me), enable Add.
    // Heuristic — definitive answer comes from the backend via
    // the actual mutation 403/200.
    canManage.value = inferCanManage()
  } catch (e) {
    error.value = friendlyError(e, 'Could not load members.')
  } finally {
    loading.value = false
  }
}

async function loadMe() {
  try { me.value = await getMe() } catch { me.value = null }
}

function inferCanManage() {
  if (!me.value) return null
  if (me.value.role === 'admin') return true
  const myMembership = members.value.find((m) => m.user_id === me.value.user_id)
  if (!myMembership) return false
  return myMembership.role === 'owner' || myMembership.role === 'rw'
}

// ── Search / suggestions ────────────────────────────────────────
let _searchSeq = 0
let _searchTimer = null
function onQueryInput() {
  pickedUser.value = null
  if (_searchTimer) clearTimeout(_searchTimer)
  const q = query.value.trim()
  if (!q) {
    suggestions.value = []
    searchLoading.value = false
    return
  }
  _searchTimer = setTimeout(() => doSearch(q), 200)
}

async function doSearch(q) {
  const seq = ++_searchSeq
  searchLoading.value = true
  try {
    const rows = await searchUsers(q, 10)
    if (seq !== _searchSeq) return    // a newer query started
    suggestions.value = rows || []
    suggestionIdx.value = 0
  } catch (e) {
    if (seq !== _searchSeq) return
    error.value = friendlyError(e, 'User search failed.')
  } finally {
    if (seq === _searchSeq) searchLoading.value = false
  }
}

function moveSuggestion(delta) {
  if (!suggestions.value.length) return
  const n = suggestions.value.length
  suggestionIdx.value = (suggestionIdx.value + delta + n) % n
}

function isAlreadyMember(s) {
  return members.value.some((m) => m.user_id === s.user_id)
}

function onPickSuggestion(s) {
  if (!s || isAlreadyMember(s)) return
  pickedUser.value = s
  query.value = ''
  suggestions.value = []
}

function clearPicked() {
  pickedUser.value = null
  nextTick(() => searchInput.value?.focus())
}

// ── Mutations ───────────────────────────────────────────────────
async function onAdd() {
  if (!pickedUser.value) return
  addLoading.value = true
  error.value = ''
  try {
    members.value = await addFolderMember(
      props.folderId,
      pickedUser.value.email,
      newRole.value,
    )
    pickedUser.value = null
    canManage.value = inferCanManage()
  } catch (e) {
    error.value = friendlyError(e, 'Could not add member.')
  } finally {
    addLoading.value = false
  }
}

async function onChangeRole(m, role) {
  if (!role || role === m.role) return
  busyId.value = m.user_id
  error.value = ''
  try {
    members.value = await updateFolderMemberRole(props.folderId, m.user_id, role)
  } catch (e) {
    error.value = friendlyError(e, "Couldn't change that member's role.")
    // Force the <select> back to the truth from the server (the
    // optimistic v-model already updated the DOM otherwise).
    await loadMembers()
  } finally {
    busyId.value = null
  }
}

async function onRemove(m) {
  busyId.value = m.user_id
  error.value = ''
  try {
    members.value = await removeFolderMember(props.folderId, m.user_id)
    canManage.value = inferCanManage()
  } catch (e) {
    error.value = friendlyError(e, "Couldn't remove that member.")
  } finally {
    busyId.value = null
  }
}

// ── Helpers ─────────────────────────────────────────────────────
function isMe(m) { return me.value && me.value.user_id === m.user_id }
function isInherited(m) { return !!(m.source && m.source.startsWith('inherited:')) }
function roleLabel(r) {
  return ({ owner: 'Owner', rw: 'Edit', r: 'View' })[r] || r
}

function friendlyError(e, fallback) {
  const status = e?.status
  const detail = (e?.message || '').toLowerCase()
  if (status === 403) return "You don't have permission to do that."
  if (status === 404 && detail.includes('user')) return 'No active user found with that email.'
  if (status === 404) return 'Folder no longer exists.'
  if (status === 409 && detail.includes('inherit')) {
    return 'This member is inherited from a parent folder — edit there instead.'
  }
  if (status === 409) return e.message
  if (status === 429) return 'Too many requests. Try again in a moment.'
  if (status >= 500) return 'The server hit an error. Try again in a moment.'
  return fallback
}
</script>

<style scoped>
/* ── Backdrop + panel ─────────────────────────────────────────── */
.dialog-backdrop {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(4px);
}
.panel {
  width: 100%;
  max-width: 440px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 12px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.32);
  overflow: hidden;
}

/* ── Header ───────────────────────────────────────────────────── */
.header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 16px 12px;
  border-bottom: 1px solid var(--color-line);
}
.title {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--color-t1);
}
.subtitle {
  margin-top: 2px;
  font-size: 0.6875rem;
  color: var(--color-t3);
}
.folder-label {
  color: var(--color-t1);
  font-family: var(--font-mono, ui-monospace, monospace);
}
.close-btn {
  background: transparent;
  border: none;
  color: var(--color-t3);
  cursor: pointer;
  padding: 4px;
  border-radius: 6px;
  display: inline-flex;
}
.close-btn:hover { color: var(--color-t1); background: var(--color-bg2); }

/* ── Add row ──────────────────────────────────────────────────── */
.add-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--color-line);
}
.search-wrap {
  position: relative;
  flex: 1;
}
.search-icon {
  position: absolute;
  left: 8px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--color-t3);
  pointer-events: none;
}
.search-spin {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  display: inline-flex;
  color: var(--color-t3);
}
.search-input {
  width: 100%;
  height: 30px;
  padding: 0 24px 0 28px;
  font-size: 0.75rem;
  border: 1px solid var(--color-line);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-t1);
  outline: none;
}
.search-input:focus { border-color: var(--color-line2); box-shadow: var(--ring-focus); }

.role-select {
  height: 30px;
  padding: 0 8px;
  font-size: 0.75rem;
  border: 1px solid var(--color-line);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-t1);
  cursor: pointer;
}
.role-select--row {
  height: 26px;
  font-size: 0.6875rem;
}

.btn-primary {
  height: 30px;
  padding: 0 12px;
  border-radius: 6px;
  border: 1px solid transparent;
  background: var(--color-t1);
  color: var(--color-bg);
  font-size: 0.75rem;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.12s ease;
}
.btn-primary:hover:not(:disabled) { opacity: 0.92; }
.btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }

.btn-secondary {
  height: 30px;
  padding: 0 14px;
  border-radius: 6px;
  border: 1px solid var(--color-line);
  background: var(--color-bg);
  color: var(--color-t1);
  font-size: 0.75rem;
  cursor: pointer;
}
.btn-secondary:hover { background: var(--color-bg2); }

/* ── Suggestions popover ──────────────────────────────────────── */
.suggestions {
  position: absolute;
  top: calc(100% - 4px);
  left: 16px;
  right: 16px;
  margin-top: 6px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  max-height: 220px;
  overflow-y: auto;
  z-index: 5;
  padding: 4px;
}
.suggestion {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 8px;
  background: transparent;
  border: none;
  border-radius: 6px;
  text-align: left;
  cursor: pointer;
  color: var(--color-t1);
}
.suggestion:hover, .suggestion.is-active { background: var(--color-bg2); }
.suggestion.is-disabled { opacity: 0.5; cursor: not-allowed; }
.suggestion-meta { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.suggestion-name {
  font-size: 0.75rem;
  color: var(--color-t1);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.suggestion-email {
  font-size: 0.625rem;
  color: var(--color-t3);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.badge-already {
  font-size: 0.625rem;
  color: var(--color-t3);
  background: var(--color-bg3);
  padding: 1px 5px;
  border-radius: 3px;
}

/* ── Picked-user chip row ─────────────────────────────────────── */
.picked-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: var(--color-bg2);
  border-bottom: 1px solid var(--color-line);
  font-size: 0.75rem;
}
.picked-name { color: var(--color-t1); font-weight: 500; }
.picked-email { color: var(--color-t3); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.picked-clear {
  background: transparent;
  border: none;
  color: var(--color-t3);
  cursor: pointer;
  padding: 2px;
  border-radius: 4px;
  display: inline-flex;
}
.picked-clear:hover { color: var(--color-t1); background: var(--color-bg3); }

/* ── Member list ──────────────────────────────────────────────── */
.members {
  flex: 1;
  overflow-y: auto;
  padding: 6px 4px;
}
.empty {
  padding: 20px 16px;
  font-size: 0.75rem;
  color: var(--color-t3);
  text-align: center;
}
.member-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 6px;
}
.member-row:hover { background: var(--color-bg2); }
.member-meta { flex: 1; min-width: 0; }
.member-name-row { display: flex; align-items: center; gap: 6px; }
.member-name {
  font-size: 0.8125rem;
  color: var(--color-t1);
  font-weight: 500;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.member-email {
  font-size: 0.6875rem;
  color: var(--color-t3);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.badge-you, .badge-owner, .badge-inherited {
  font-size: 0.625rem;
  font-weight: 500;
  padding: 1px 5px;
  border-radius: 3px;
}
.badge-you { background: var(--color-bg3); color: var(--color-t2); }
.badge-owner {
  background: color-mix(in srgb, var(--color-accent, #6366f1) 12%, transparent);
  color: var(--color-accent, #6366f1);
}
.badge-inherited {
  background: var(--color-bg3);
  color: var(--color-t3);
}
.role-static {
  font-size: 0.6875rem;
  color: var(--color-t3);
  padding: 0 6px;
}
.btn-remove {
  background: transparent;
  border: none;
  color: var(--color-t3);
  cursor: pointer;
  padding: 4px;
  border-radius: 4px;
  display: inline-flex;
}
.btn-remove:hover:not(:disabled) {
  color: var(--color-err-fg, #b91c1c);
  background: color-mix(in srgb, #ef4444 8%, transparent);
}
.btn-remove:disabled { opacity: 0.45; cursor: not-allowed; }

/* ── Error + footer ───────────────────────────────────────────── */
.error {
  margin: 0 16px 12px;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 0.6875rem;
  line-height: 1.45;
  color: var(--color-err-fg, #d23);
  background: var(--color-err-bg, rgba(214, 60, 50, 0.08));
  border: 1px solid var(--color-err-line, rgba(214, 60, 50, 0.25));
}
.footer {
  display: flex;
  justify-content: flex-end;
  padding: 12px 16px;
  border-top: 1px solid var(--color-line);
  background: var(--color-bg);
}

/* ── Enter / leave transition ─────────────────────────────────── */
.dialog-enter-active, .dialog-leave-active { transition: opacity 0.15s ease; }
.dialog-enter-active .panel,
.dialog-leave-active .panel { transition: transform 0.15s ease, opacity 0.15s ease; }
.dialog-enter-from, .dialog-leave-to { opacity: 0; }
.dialog-enter-from .panel,
.dialog-leave-to .panel { transform: translateY(8px); opacity: 0; }
</style>
