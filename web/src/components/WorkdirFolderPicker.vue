<template>
  <!--
    Workdir folder picker — modal that lets the user browse the
    workdir tree and pick a folder. Sibling of ``FolderPickerDialog``;
    that one is tied to the Library's flat ``listFolders()`` API, this
    one walks the workdir tree via ``listWorkdir(path)`` (lazy
    per-folder children fetches, cached so navigating back doesn't
    re-hit the server).

    The "select" action emits the currently-displayed folder's path —
    same semantic as Finder's "Choose this folder" button.
  -->
  <Teleport to="body">
    <div
      v-if="open"
      class="dialog-backdrop"
      @click.self="onCancel"
      @keydown.esc="onCancel"
    >
      <div class="picker panel" role="dialog" aria-modal="true" tabindex="-1" ref="dialogEl">
          <div class="picker__header">
            <h2 class="picker__title">{{ title }}</h2>
            <p v-if="description" class="picker__desc">{{ description }}</p>
          </div>

          <div class="picker__crumb">
            <button
              v-for="(seg, idx) in breadcrumb"
              :key="seg.path"
              class="picker__crumb-btn"
              :disabled="idx === breadcrumb.length - 1"
              @click="navigateTo(seg.path)"
            >{{ seg.label }}</button>
            <span class="picker__crumb-spacer"></span>
            <button
              class="picker__newfolder-btn"
              :title="t('chat.workdir_picker.new_folder')"
              :disabled="creating || mkdirBusy"
              @click="startCreate"
            >
              <FolderPlus :size="13" :stroke-width="1.75" />
              <span>{{ t('chat.workdir_picker.new_folder') }}</span>
            </button>
          </div>

          <div class="picker__body">
            <!-- Inline new-folder editor at the top of the body —
                 same affordance as the workspace's create flow.
                 Confirm on Enter / blur, cancel on Esc. -->
            <div v-if="creating" class="picker__row picker__row--creating">
              <FileIcon kind="folder" :size="16" />
              <input
                ref="newNameInput"
                v-model="newName"
                type="text"
                class="picker__newfolder-input"
                :placeholder="t('chat.workdir_picker.new_folder_placeholder')"
                :disabled="mkdirBusy"
                @keydown.enter.prevent="confirmCreate"
                @keydown.esc.prevent="cancelCreate"
                @blur="confirmCreate"
              />
            </div>

            <div v-if="loading" class="picker__hint">{{ t('common.loading') }}</div>
            <div v-else-if="error" class="picker__hint picker__hint--error">{{ error }}</div>
            <div v-else-if="!creating && visibleFolders.length === 0" class="picker__hint">
              {{ t('chat.workdir_picker.no_subfolders', { path: currentPath }) }}
            </div>
            <button
              v-for="f in visibleFolders"
              :key="f.path"
              class="picker__row"
              @click="enter(f.path)"
            >
              <FileIcon kind="folder" :size="16" />
              <span class="picker__row-name">{{ f.name }}</span>
              <ChevronRight class="picker__row-chev" :size="14" :stroke-width="1.5" />
            </button>
          </div>

          <div class="picker__footer">
            <span class="picker__current">
              {{ t('chat.workdir_picker.selected') }} <b>{{ currentPath }}</b>
            </span>
            <div class="picker__actions">
              <button class="btn-secondary" @click="onCancel">{{ t('common.cancel') }}</button>
              <button
                v-if="allowClear && currentPath !== '/'"
                class="btn-secondary"
                @click="onClear"
              >{{ t('chat.workdir_picker.use_root') }}</button>
              <button class="btn-primary" @click="onConfirm">{{ confirmText }}</button>
            </div>
          </div>
        </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronRight, FolderPlus } from 'lucide-vue-next'
import { listWorkdir, makeWorkdirFolder } from '@/api'
import { useDialog } from '@/composables/useDialog'
import FileIcon from '@/components/workspace/FileIcon.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: '' },
  description: { type: String, default: '' },
  confirmText: { type: String, default: 'Select' },
  initialPath: { type: String, default: '/' },
  // When true, show a "Use workdir root" shortcut so the user can
  // pick the implicit root without having to navigate up.
  allowClear: { type: Boolean, default: false },
})
const emit = defineEmits(['update:open', 'select', 'cancel', 'clear'])

const { t } = useI18n()
const dialog = useDialog()

const dialogEl = ref(null)
const currentPath = ref('/')
const loading = ref(false)
const error = ref('')

// Inline new-folder editor state. ``creating`` toggles a row at the
// top of the body that's an input field; on Enter/blur we POST and
// then re-fetch the current folder's children so the new entry shows
// up in the listing (and the user can drill into it). Errors surface
// via the global alert dialog rather than inline so they don't shift
// the picker layout.
const creating = ref(false)
const mkdirBusy = ref(false)
const newName = ref('')
const newNameInput = ref(null)
let _createFired = false

// Cache of path -> subfolder list. Avoids re-fetching on navigate-back.
// Cleared on dialog open so the snapshot matches what's currently in
// the workdir (the agent / other sessions may have mutated it). Held
// as a plain object behind a ``ref`` (rather than a raw Map) because
// Vue's reactivity tracks ``ref.value`` reassignment but not Map
// mutation — without this, ``visibleFolders`` would stay stale after
// the first fetch.
const cache = ref({})

watch(() => props.open, async (now) => {
  if (now) {
    cache.value = {}
    error.value = ''
    creating.value = false
    newName.value = ''
    currentPath.value = props.initialPath || '/'
    await fetchChildren(currentPath.value)
    await nextTick()
    dialogEl.value?.focus()
  }
})

async function fetchChildren(path) {
  if (path in cache.value) return
  loading.value = true
  error.value = ''
  try {
    const list = await listWorkdir(path === '/' ? '' : path)
    const folders = list.filter(e => e.is_dir)
    cache.value = { ...cache.value, [path]: folders }
  } catch (e) {
    cache.value = { ...cache.value, [path]: [] }
    error.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

const visibleFolders = computed(() => cache.value[currentPath.value] || [])

const breadcrumb = computed(() => {
  if (currentPath.value === '/') return [{ path: '/', label: t('chat.workdir_picker.root') }]
  const parts = currentPath.value.split('/').filter(Boolean)
  const segs = [{ path: '/', label: t('chat.workdir_picker.root') }]
  let acc = ''
  for (const p of parts) {
    acc += '/' + p
    segs.push({ path: acc, label: p })
  }
  return segs
})

async function navigateTo(path) {
  currentPath.value = path
  await fetchChildren(path)
}

async function enter(path) {
  currentPath.value = path
  await fetchChildren(path)
}

async function startCreate() {
  creating.value = true
  newName.value = ''
  await nextTick()
  newNameInput.value?.focus()
}

function cancelCreate() {
  // Trip the guard so the input's blur-on-unmount doesn't fire confirm.
  _createFired = true
  creating.value = false
  newName.value = ''
  setTimeout(() => { _createFired = false }, 0)
}

async function confirmCreate() {
  if (_createFired) return
  _createFired = true
  setTimeout(() => { _createFired = false }, 0)
  const name = (newName.value || '').trim()
  if (!name) {
    creating.value = false
    return
  }
  if (name.includes('/') || name.includes('\\') || name.startsWith('.')) {
    dialog.alert({
      title: t('chat.workdir_picker.new_folder_error_title'),
      description: t('chat.workdir_picker.new_folder_error_invalid'),
    })
    return
  }
  const parent = currentPath.value
  const newPath = (parent === '/' ? '' : parent.replace(/\/+$/, '')) + '/' + name
  mkdirBusy.value = true
  try {
    await makeWorkdirFolder(newPath)
    creating.value = false
    newName.value = ''
    // Drop the cached listing so fetchChildren re-hits the API and
    // the new folder appears in the row list.
    const cur = currentPath.value
    const next = { ...cache.value }
    delete next[cur]
    cache.value = next
    await fetchChildren(cur)
  } catch (e) {
    dialog.alert({
      title: t('chat.workdir_picker.new_folder_error_title'),
      description: e?.message || String(e),
    })
  } finally {
    mkdirBusy.value = false
  }
}

function onConfirm() {
  emit('select', currentPath.value)
  emit('update:open', false)
}

function onClear() {
  emit('clear')
  emit('update:open', false)
}

function onCancel() {
  emit('cancel')
  emit('update:open', false)
}
</script>

<style scoped>
/* Same shell as FolderPickerDialog so the two pickers feel like one
   component. Couldn't use ``@import`` cleanly across scoped styles, so
   the rules are duplicated; if a third picker shows up we'll factor
   the shell into a base component. */
.dialog-backdrop {
  position: fixed;
  inset: 0;
  background: color-mix(in srgb, #000 45%, transparent);
  backdrop-filter: blur(2px);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.picker {
  width: 100%;
  max-width: 480px;
  max-height: 70vh;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
  outline: none;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
  border: 1px solid var(--color-line);
  border-radius: 8px;
  overflow: hidden;
}
.picker__header { padding: 16px 18px 8px; }
.picker__title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-t1);
  margin: 0;
  letter-spacing: -0.01em;
}
.picker__desc {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--color-t2);
  line-height: 1.55;
}
.picker__crumb {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border-top: 1px solid var(--color-line);
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  overflow-x: auto;
  white-space: nowrap;
}
.picker__crumb-btn {
  padding: 3px 7px;
  font-size: 11px;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
}
.picker__crumb-btn:hover:not(:disabled) {
  background: var(--color-bg);
  color: var(--color-t1);
}
.picker__crumb-btn:disabled {
  color: var(--color-t1);
  font-weight: 500;
  cursor: default;
}
.picker__crumb-btn:not(:last-of-type)::after {
  content: '/';
  margin-left: 8px;
  color: var(--color-t3);
}
.picker__crumb-spacer { flex: 1; }
.picker__newfolder-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  font-size: 11px;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  white-space: nowrap;
}
.picker__newfolder-btn:hover:not(:disabled) {
  color: var(--color-t1);
  background: var(--color-bg);
}
.picker__newfolder-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.picker__row--creating,
.picker__row--creating:hover { background: var(--color-bg2); cursor: default; }
.picker__newfolder-input {
  flex: 1;
  min-width: 0;
  padding: 2px 6px;
  font-size: 12px;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line2);
  border-radius: var(--r-sm);
  outline: none;
  box-shadow: var(--ring-focus);
}
.picker__body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
  min-height: 200px;
}
.picker__hint {
  padding: 18px 18px;
  font-size: 12px;
  color: var(--color-t3);
  text-align: center;
}
.picker__hint--error { color: var(--color-err-fg); }
.picker__row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 16px;
  font-size: 12px;
  color: var(--color-t1);
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
}
.picker__row:hover { background: var(--color-bg2); }
.picker__row-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.picker__row-chev { color: var(--color-t3); }
.picker__footer {
  padding: 10px 16px;
  border-top: 1px solid var(--color-line);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: var(--color-bg2);
}
.picker__current {
  font-size: 11px;
  color: var(--color-t2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.picker__current b {
  color: var(--color-t1);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-weight: 500;
}
.picker__actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.btn-primary, .btn-secondary {
  padding: 5px 11px;
  font-size: 11px;
  border-radius: 5px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.btn-secondary {
  color: var(--color-t2);
  background: transparent;
  border: 1px solid var(--color-line);
}
.btn-secondary:hover {
  color: var(--color-t1);
  background: var(--color-bg);
}
.btn-primary {
  color: white;
  background: var(--color-brand);
  border: 1px solid var(--color-brand);
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
