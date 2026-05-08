<!--
  LibraryDocPicker — modal that lets the user pick a Library document
  and copy it into the current project's workdir.

  Phase 1 manual UI counterpart to the Phase 2 ``import_from_library``
  agent tool (same backend endpoint, same authz). Lists docs the
  caller already has read access to in the Library — the existing
  multi-user folder-grant filtering applies, so a user can only see
  / pick docs they could open in the Library UI.

  Search is server-side (`/api/v1/documents?search=...`); we debounce
  300ms so type-ahead doesn't hammer the backend.

  On click → POST /projects/{id}/import → toast (with "reused" hint
  when the artifact already existed) → close modal. The browser
  parent calls ``reload()`` on close so the imported file shows up
  immediately in the file list.
-->
<template>
  <Teleport to="body">
    <div
      class="picker-backdrop"
      @click.self="onClose"
      @keydown.esc="onClose"
    >
      <div class="picker-panel" role="dialog" aria-modal="true">
        <header class="picker-header">
          <div>
            <div class="picker-title">{{ t('workspace.picker.title') }}</div>
            <div class="picker-subtitle">
              {{ t('workspace.picker.subtitle', { name: projectName }) }}
            </div>
          </div>
          <button class="picker-close" @click="onClose" aria-label="Close">
            <X :size="16" :stroke-width="1.75" />
          </button>
        </header>

        <div class="picker-search">
          <Search :size="14" :stroke-width="1.75" class="picker-search__icon" />
          <input
            ref="searchInput"
            v-model="query"
            class="picker-search__input"
            :placeholder="t('workspace.picker.search_placeholder')"
            @input="onQueryInput"
          />
        </div>

        <div v-if="error" class="picker-error">
          <AlertCircle :size="14" :stroke-width="1.75" />
          <span>{{ error }}</span>
        </div>

        <div class="picker-results">
          <div v-if="loading" class="picker-state">
            <Skeleton v-for="i in 4" :key="i" class="picker-skel" />
          </div>

          <div v-else-if="!results.length" class="picker-state picker-state--empty">
            <BookOpen :size="24" :stroke-width="1.25" />
            <p v-if="query">{{ t('workspace.picker.no_results', { q: query }) }}</p>
            <p v-else>{{ t('workspace.picker.empty') }}</p>
          </div>

          <ul v-else class="picker-list">
            <li
              v-for="doc in results"
              :key="doc.doc_id"
              class="picker-row"
              :class="{ 'picker-row--busy': importingId === doc.doc_id }"
              @click="onImport(doc)"
            >
              <span class="picker-row__icon">
                <FileIcon :size="14" :stroke-width="1.75" />
              </span>
              <span class="picker-row__meta">
                <span class="picker-row__name">{{ doc.filename }}</span>
                <span class="picker-row__path">{{ doc.path || '/' }}</span>
              </span>
              <span class="picker-row__action">
                <Loader2
                  v-if="importingId === doc.doc_id"
                  :size="14"
                  :stroke-width="1.75"
                  class="picker-spin"
                />
                <ArrowDownToLine
                  v-else
                  :size="14"
                  :stroke-width="1.75"
                />
              </span>
            </li>
          </ul>
        </div>

        <footer class="picker-footer">
          <p class="picker-footnote">{{ t('workspace.picker.footnote') }}</p>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  AlertCircle,
  ArrowDownToLine,
  BookOpen,
  File as FileIcon,
  Loader2,
  Search,
  X,
} from 'lucide-vue-next'

import { importDocFromLibrary, listDocuments } from '@/api'
import Skeleton from '@/components/Skeleton.vue'
import { useDialog } from '@/composables/useDialog'

const { t } = useI18n()
const dialog = useDialog()

const props = defineProps({
  projectId: { type: String, required: true },
  projectName: { type: String, default: '' },
})
const emit = defineEmits(['close', 'imported'])

const query = ref('')
const results = ref([])
const loading = ref(false)
const error = ref('')
const importingId = ref('')
const searchInput = ref(null)

let _debounce = null

function onQueryInput() {
  if (_debounce) clearTimeout(_debounce)
  // Slightly longer than the 200ms feel-good number — listDocuments
  // hits the BM25 + folder-filter path which is fast but typing
  // multi-char queries is more expensive than one nice word.
  _debounce = setTimeout(loadResults, 300)
}

async function loadResults() {
  loading.value = true
  error.value = ''
  try {
    const params = { limit: 30 }
    const q = query.value.trim()
    if (q) params.search = q
    const out = await listDocuments(params)
    results.value = out?.items || []
  } catch (e) {
    error.value = e?.message || String(e)
    results.value = []
  } finally {
    loading.value = false
  }
}

async function onImport(doc) {
  if (importingId.value) return
  importingId.value = doc.doc_id
  try {
    const r = await importDocFromLibrary(props.projectId, doc.doc_id)
    if (r.reused) {
      dialog.toast(
        t('workspace.picker.imported_reused', {
          name: doc.filename,
          path: r.target_path,
        }),
        { variant: 'info' },
      )
    } else {
      dialog.toast(
        t('workspace.picker.imported', {
          name: doc.filename,
          path: r.target_path,
        }),
        { variant: 'success' },
      )
    }
    emit('imported', r)
    emit('close')
  } catch (e) {
    error.value = e?.message || String(e)
  } finally {
    importingId.value = ''
  }
}

function onClose() {
  emit('close')
}

function onKeyDown(e) {
  if (e.key === 'Escape') onClose()
}

onMounted(() => {
  loadResults()
  // Focus search after the modal animation settles.
  setTimeout(() => searchInput.value?.focus(), 50)
  document.addEventListener('keydown', onKeyDown)
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeyDown)
  if (_debounce) clearTimeout(_debounce)
})
</script>

<style scoped>
.picker-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.picker-panel {
  width: min(620px, 92vw);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  background: var(--surface, #fff);
  border-radius: 12px;
  box-shadow: 0 20px 50px rgba(0, 0, 0, 0.2);
  overflow: hidden;
}

.picker-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border, #e5e7eb);
}

.picker-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text, #111827);
}

.picker-subtitle {
  margin-top: 2px;
  font-size: 12.5px;
  color: var(--text-muted, #6b7280);
}

.picker-close {
  background: transparent;
  border: 0;
  padding: 4px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text-muted, #6b7280);
}

.picker-close:hover {
  background: var(--surface-muted, #f3f4f6);
  color: var(--text, #111827);
}

.picker-search {
  position: relative;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border, #e5e7eb);
}

.picker-search__icon {
  position: absolute;
  left: 30px;
  top: 50%;
  transform: translateY(-50%);
  color: var(--text-muted, #6b7280);
}

.picker-search__input {
  width: 100%;
  height: 32px;
  padding: 0 10px 0 32px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 6px;
  font-size: 13px;
  background: var(--surface, #fff);
}

.picker-search__input:focus {
  outline: none;
  border-color: var(--accent, #111827);
}

.picker-error {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 20px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(220, 38, 38, 0.08);
  color: var(--danger, #b91c1c);
  font-size: 12.5px;
}

.picker-results {
  flex: 1;
  min-height: 200px;
  max-height: 50vh;
  overflow-y: auto;
}

.picker-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 16px;
  color: var(--text-muted, #6b7280);
  text-align: center;
}

.picker-state--empty p {
  font-size: 13px;
  margin: 0;
}

.picker-skel {
  width: 92%;
  height: 36px;
  margin: 6px auto;
  border-radius: 4px;
}

.picker-list {
  list-style: none;
  margin: 0;
  padding: 4px 0;
}

.picker-row {
  display: grid;
  grid-template-columns: 24px 1fr auto;
  align-items: center;
  gap: 12px;
  padding: 8px 20px;
  cursor: pointer;
}

.picker-row:hover {
  background: var(--surface-muted, #f3f4f6);
}

.picker-row--busy {
  opacity: 0.6;
  cursor: wait;
}

.picker-row__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted, #6b7280);
}

.picker-row__meta {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.picker-row__name {
  font-size: 13px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text, #111827);
}

.picker-row__path {
  font-size: 11.5px;
  color: var(--text-muted, #6b7280);
  font-family: var(--mono, ui-monospace, SFMono-Regular, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.picker-row__action {
  color: var(--accent, #111827);
}

.picker-spin {
  animation: picker-spin 0.8s linear infinite;
}

@keyframes picker-spin {
  to { transform: rotate(360deg); }
}

.picker-footer {
  padding: 10px 20px;
  border-top: 1px solid var(--border, #e5e7eb);
  background: var(--surface-muted, #fafafa);
}

.picker-footnote {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-muted, #6b7280);
  line-height: 1.4;
}
</style>
