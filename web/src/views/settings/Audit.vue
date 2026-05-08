<script setup>
/**
 * /settings/audit — admin-only activity feed.
 *
 * Reads the append-only audit_log table. Every folder / document /
 * trash / membership / admin user-management mutation lands a row
 * with the actor's principal.user_id. This page surfaces that data
 * for forensic / compliance use ("what did Alice do last week?",
 * "who deleted the Q4 contract?").
 *
 * Layout: filter strip on top (action category + actor + free-text
 * date), paginated table below. No bulk actions — audit_log is
 * append-only by design.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { listAuditLog, avatarUrlFor } from '@/api/admin'
import { useDialog } from '@/composables/useDialog'
import { Search, RefreshCw } from 'lucide-vue-next'
import UserAvatar from '@/components/UserAvatar.vue'

const { toast } = useDialog()

// ── State ────────────────────────────────────────────────────────
const items = ref([])
const total = ref(0)
const loading = ref(false)

const PAGE_SIZE = 50
const offset = ref(0)
const filterCategory = ref('all')   // 'all' | 'folder' | 'document' | 'auth'
const filterActor = ref('')          // exact user_id (paste from a row)
const filterQuery = ref('')          // client-side fuzzy match on action / target_id / details

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const currentPage = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)

// ── Loaders ──────────────────────────────────────────────────────
async function load() {
  loading.value = true
  try {
    const params = { limit: PAGE_SIZE, offset: offset.value }
    if (filterCategory.value !== 'all') {
      params.action_prefix = filterCategory.value + '.'
    }
    if (filterActor.value) {
      params.actor_id = filterActor.value
    }
    const r = await listAuditLog(params)
    items.value = r?.items || []
    total.value = r?.total || 0
  } catch (e) {
    toast(`Could not load audit log: ${e.message || e}`, { variant: 'error' })
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

// Reset offset when filters change so the user always sees page 1
// of the new filter rather than an empty later page.
watch([filterCategory, filterActor], () => {
  offset.value = 0
  load()
})

onMounted(load)

// ── Client-side text filter (cheap; runs on the page only) ───────
const visibleItems = computed(() => {
  const q = filterQuery.value.trim().toLowerCase()
  if (!q) return items.value
  return items.value.filter((r) => {
    const hay = [
      r.action,
      r.target_id,
      r.actor_username,
      r.actor_display_name,
      r.actor_email,
      r.target_type,
      r.details ? JSON.stringify(r.details) : '',
    ].filter(Boolean).join(' ').toLowerCase()
    return hay.includes(q)
  })
})

// ── Helpers ──────────────────────────────────────────────────────
function actorLabel(r) {
  if (!r.actor_id) return 'unknown'
  if (r.actor_id === 'system') return 'system'
  if (r.actor_id === 'local') return 'local (legacy)'
  return r.actor_display_name || r.actor_username || r.actor_id
}

function actorEmail(r) {
  if (!r.actor_id || r.actor_id === 'system' || r.actor_id === 'local') return ''
  return r.actor_email || ''
}

function actionLabel(action) {
  // human-readable rewrite for the most common ones
  const map = {
    'folder.create':       'Created folder',
    'folder.rename':       'Renamed folder',
    'folder.move':         'Moved folder',
    'folder.trash':        'Deleted folder',
    'folder.restore':      'Restored folder',
    'folder.share':        'Shared folder',
    'folder.unshare':      'Removed from folder',
    'folder.update_role':  'Changed folder role',
    'document.move':       'Moved document',
    'document.rename':     'Renamed document',
    'document.trash':      'Deleted document',
    'document.purge':      'Permanently deleted',
    'document.restore':    'Restored document',
    'auth.user_approve':   'Approved user',
    'auth.user_suspend':   'Suspended user',
    'auth.user_reactivate':'Reactivated user',
    'auth.user_role':      'Changed user role',
    'auth.user_delete':    'Deleted user',
  }
  return map[action] || action
}

function actionCategory(action) {
  if (!action) return ''
  return action.split('.', 1)[0]
}

function targetLabel(r) {
  // documents have UUID-ish target_ids — show them truncated.
  // folders have folder_id but the path is in details.path.
  if (r.target_type === 'folder' && r.details?.path) return r.details.path
  if (r.target_type === 'document' && r.details?.new_path) return r.details.new_path
  if (r.target_type === 'document' && r.details?.to_path) return r.details.to_path
  if (r.target_id) return r.target_id
  return ''
}

function detailsSummary(r) {
  if (!r.details) return ''
  const d = r.details
  const interesting = []
  if (d.old_path && d.new_path) interesting.push(`${d.old_path} → ${d.new_path}`)
  else if (d.from_path && d.to_path) interesting.push(`${d.from_path} → ${d.to_path}`)
  if (d.role) interesting.push(`role: ${d.role}`)
  if (d.user_id) interesting.push(`target user: ${d.user_id}`)
  if (interesting.length) return interesting.join(' · ')
  // Fallback: short JSON, capped so the row stays one line.
  const s = JSON.stringify(d)
  return s.length > 120 ? s.slice(0, 117) + '…' : s
}

function fmtTime(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return d.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch { return iso }
}

function prevPage() {
  if (offset.value <= 0) return
  offset.value = Math.max(0, offset.value - PAGE_SIZE)
  load()
}
function nextPage() {
  if (offset.value + PAGE_SIZE >= total.value) return
  offset.value = offset.value + PAGE_SIZE
  load()
}

function clearActorFilter() { filterActor.value = '' }
function setActorFilter(actorId) { filterActor.value = actorId }
</script>

<template>
  <div class="audit-page">
    <header class="page-header">
      <div>
        <h2 class="page-title">Activity</h2>
        <p class="page-subtitle">
          Append-only log of every workspace mutation —
          who did what, when, and to which resource.
        </p>
      </div>
      <button class="refresh-btn" :disabled="loading" @click="load" title="Refresh">
        <RefreshCw :size="14" :stroke-width="1.75" :class="{ spin: loading }" />
      </button>
    </header>

    <!-- ── Filter strip ── -->
    <div class="toolbar">
      <div class="filter-chips">
        <button
          v-for="f in [
            { key: 'all',      label: 'All' },
            { key: 'folder',   label: 'Folders' },
            { key: 'document', label: 'Documents' },
            { key: 'auth',     label: 'Users' },
          ]"
          :key="f.key"
          class="chip"
          :class="{ 'is-active': filterCategory === f.key }"
          @click="filterCategory = f.key"
        >{{ f.label }}</button>
      </div>

      <div v-if="filterActor" class="actor-pill">
        <span>Actor: {{ filterActor }}</span>
        <button class="actor-pill-x" @click="clearActorFilter" aria-label="Clear actor filter">×</button>
      </div>

      <div class="search-wrap">
        <Search :size="14" :stroke-width="1.75" class="search-icon" />
        <input
          v-model="filterQuery"
          class="search-input"
          placeholder="Filter by action, target, or detail…"
        />
      </div>
    </div>

    <!-- ── Table ── -->
    <div class="table">
      <div class="table-head">
        <div class="col-when">When</div>
        <div class="col-who">Who</div>
        <div class="col-what">Action</div>
        <div class="col-target">Target</div>
        <div class="col-detail">Details</div>
      </div>

      <div v-if="loading && !items.length" class="empty">Loading…</div>
      <div v-else-if="!visibleItems.length" class="empty">
        {{ items.length ? 'No rows match the text filter.' : 'No activity yet.' }}
      </div>

      <div
        v-for="r in visibleItems"
        :key="r.audit_id"
        class="row"
      >
        <div class="col-when" :title="r.created_at">{{ fmtTime(r.created_at) }}</div>

        <div class="col-who">
          <UserAvatar
            :name="actorLabel(r)"
            :img-url="r.actor_id !== 'system' && r.actor_id !== 'local' ? avatarUrlFor(r.actor_id, false) : null"
            :size="22"
          />
          <button
            type="button"
            class="actor-name"
            :title="`Filter to ${actorLabel(r)}'s actions`"
            @click="setActorFilter(r.actor_id)"
          >
            <span class="actor-line">{{ actorLabel(r) }}</span>
            <span v-if="actorEmail(r)" class="actor-email">{{ actorEmail(r) }}</span>
          </button>
        </div>

        <div class="col-what">
          <span class="action-pill" :class="`action-${actionCategory(r.action)}`">
            {{ actionLabel(r.action) }}
          </span>
        </div>

        <div class="col-target">
          <code v-if="targetLabel(r)" class="target">{{ targetLabel(r) }}</code>
          <span v-else class="dim">—</span>
        </div>

        <div class="col-detail">{{ detailsSummary(r) }}</div>
      </div>
    </div>

    <!-- ── Pager ── -->
    <div class="pager" v-if="total > PAGE_SIZE">
      <span class="pager-info">
        Page {{ currentPage }} of {{ totalPages }} · {{ total }} entries
      </span>
      <button class="pager-btn" :disabled="loading || offset === 0" @click="prevPage">‹ Prev</button>
      <button
        class="pager-btn"
        :disabled="loading || offset + PAGE_SIZE >= total"
        @click="nextPage"
      >Next ›</button>
    </div>
  </div>
</template>

<style scoped>
.audit-page { width: 100%; }

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 20px;
}
.page-title {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--color-t1);
  margin: 0 0 4px;
}
.page-subtitle {
  font-size: 12px;
  color: var(--color-t3);
  margin: 0;
  max-width: 540px;
}
.refresh-btn {
  height: 30px;
  width: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  color: var(--color-t2);
  cursor: pointer;
}
.refresh-btn:hover { background: var(--color-bg2); color: var(--color-t1); }
.refresh-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.spin { animation: spin 0.6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Toolbar (filters + search) ─────────────────────────────── */
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.filter-chips { display: flex; gap: 4px; }
.chip {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: background-color .12s, color .12s, border-color .12s;
}
.chip:hover { background: var(--color-bg2); color: var(--color-t1); }
.chip.is-active {
  background: var(--color-bg2);
  border-color: var(--color-line);
  color: var(--color-t1);
  font-weight: 500;
}

.actor-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 8px 0 10px;
  font-size: 11px;
  color: var(--color-t2);
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
}
.actor-pill-x {
  background: transparent;
  border: none;
  color: var(--color-t3);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  padding: 0 4px;
}
.actor-pill-x:hover { color: var(--color-t1); }

.search-wrap {
  position: relative;
  flex: 1;
  min-width: 200px;
  max-width: 360px;
  margin-left: auto;
}
.search-icon {
  position: absolute;
  left: 8px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--color-t3);
  pointer-events: none;
}
.search-input {
  width: 100%;
  height: 28px;
  padding: 0 8px 0 28px;
  font-size: 12px;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  background: var(--color-bg);
  color: var(--color-t1);
  outline: none;
}
.search-input:focus { border-color: var(--color-line2); box-shadow: var(--ring-focus); }

/* ── Table ──────────────────────────────────────────────────── */
.table {
  display: block;
  width: 100%;
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  background: var(--color-bg);
  overflow: hidden;
}
.table-head, .row {
  display: grid;
  grid-template-columns: 160px 200px 160px minmax(180px, 1fr) minmax(160px, 1.4fr);
  gap: 12px;
  align-items: center;
  padding: 0 16px;
}
.table-head {
  height: 36px;
  background: var(--color-bg2);
  border-bottom: 1px solid var(--color-line);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--color-t3);
}
.row {
  min-height: 48px;
  padding-top: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--color-line);
  font-size: 12px;
}
.row:last-child { border-bottom: none; }
.row:hover { background: var(--color-bg2); }

.col-when { color: var(--color-t3); font-variant-numeric: tabular-nums; white-space: nowrap; }
.col-who {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.actor-name {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: transparent;
  border: none;
  padding: 0;
  text-align: left;
  cursor: pointer;
  color: var(--color-t1);
}
.actor-name:hover .actor-line { text-decoration: underline; }
.actor-line {
  font-weight: 500;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.actor-email {
  font-size: 10px;
  color: var(--color-t3);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* Action pill colour by category. Folder = neutral, document =
   teal-ish, auth = amber. Same rule we use elsewhere: colour for
   meaningful state, neutral for everything else. */
.action-pill {
  display: inline-flex;
  align-items: center;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 3px;
  background: var(--color-bg3);
  color: var(--color-t2);
  white-space: nowrap;
}
.action-pill.action-document {
  background: color-mix(in srgb, #14b8a6 14%, transparent);
  color: #0f766e;
}
.action-pill.action-auth {
  background: color-mix(in srgb, #f59e0b 14%, transparent);
  color: #b45309;
}

.target {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
  background: var(--color-bg2);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--color-t2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  max-width: 100%;
  display: inline-block;
}
.dim { color: var(--color-t3); }
.col-detail {
  color: var(--color-t3);
  font-size: 11px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.empty {
  padding: 32px;
  text-align: center;
  color: var(--color-t3);
  font-size: 12px;
}

/* ── Pager ──────────────────────────────────────────────────── */
.pager {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 12px;
  margin-top: 14px;
  font-size: 12px;
  color: var(--color-t3);
}
.pager-info { margin-right: auto; }
.pager-btn {
  height: 28px;
  padding: 0 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  color: var(--color-t1);
  font-size: 12px;
  cursor: pointer;
}
.pager-btn:hover:not(:disabled) { background: var(--color-bg2); }
.pager-btn:disabled { opacity: 0.4; cursor: not-allowed; }
</style>
