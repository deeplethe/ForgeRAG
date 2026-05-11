<template>
  <!-- Single-row toolbar: [breadcrumb slot] ... [actions on the right].
       Breadcrumb arrives via the ``lead`` slot so the parent owns nav
       state without us re-passing crumbs through props.

       Padding is in **literal px** (not Tailwind's rem-based ``p-*``
       scale) because the Workspace/Workbench top bar shares this
       layout and writes its own padding in raw px. Without the
       arbitrary-px override, ``px-5 py-3`` would ride ``--ui-scale``
       and the two pages' bars would diverge by a couple of px every
       time the global density knob moves. ``min-h-[52px]`` keeps
       trash-mode (no search input) from shrinking the row.  -->
  <div class="flex items-center gap-1 px-[20px] py-[12px] border-b border-line bg-bg2 min-h-[52px]">
    <slot name="lead" />

    <div class="flex-1"></div>

    <!-- Browse-mode actions: hidden in trash because none of them
         (create / upload / search / view-toggle / open-trash) make
         sense while the user is looking at deleted items. -->
    <template v-if="!viewingTrash">
      <button class="toolbar-btn" @click="$emit('new-folder')" title="New folder (Ctrl+N)">
        <FolderPlus :size="14" :stroke-width="1.5" />
        <span>New folder</span>
      </button>
      <!-- Upload — split-style button: main click = files picker,
           ▾ opens a tiny menu with "Upload folder" (which preserves
           the subfolder structure under the current dir). Drag-drop
           on the workspace shell handles both files and folders too. -->
      <div class="upload-split" :class="{ 'upload-split--open': uploadMenuOpen }">
        <button
          class="toolbar-btn upload-split__main"
          @click="$emit('upload')"
          title="Upload files"
        >
          <Upload :size="14" :stroke-width="1.5" />
          <span>Upload</span>
        </button>
        <button
          class="upload-split__chev"
          @click.stop="uploadMenuOpen = !uploadMenuOpen"
          title="More upload options"
        >
          <ChevronDown :size="12" :stroke-width="1.75" />
        </button>
        <Transition name="popup">
          <div v-if="uploadMenuOpen" class="upload-split__menu" @click.stop>
            <button class="upload-split__item" @click="onPickFiles">
              <Upload :size="14" :stroke-width="1.5" />
              <div>
                <div class="upload-split__title">Upload files</div>
                <div class="upload-split__desc">Pick one or more files</div>
              </div>
            </button>
            <button class="upload-split__item" @click="onPickFolder">
              <FolderUp :size="14" :stroke-width="1.5" />
              <div>
                <div class="upload-split__title">Upload folder</div>
                <div class="upload-split__desc">Pick a folder; subfolders preserved</div>
              </div>
            </button>
          </div>
        </Transition>
      </div>

      <!-- Search — filters the current folder's children -->
      <div class="search-wrap ml-2">
        <Search class="search-icon" :size="14" :stroke-width="1.5" />
        <input
          :value="search"
          @input="$emit('update:search', $event.target.value)"
          placeholder="Search this folder…"
          class="search-input"
        />
        <button
          v-if="search"
          class="search-clear"
          @click="$emit('update:search', '')"
          title="Clear"
        ><X :size="12" :stroke-width="1.5" /></button>
      </div>

      <!-- View mode switcher -->
      <div class="flex items-center gap-0.5 p-0.5 border border-line rounded-md ml-2">
        <button
          class="view-btn"
          :class="{ 'view-btn--active': viewMode === 'grid' }"
          @click="$emit('set-view', 'grid')"
          title="Grid view (Ctrl+1)"
        ><LayoutGrid :size="14" :stroke-width="1.5" /></button>
        <button
          class="view-btn"
          :class="{ 'view-btn--active': viewMode === 'list' }"
          @click="$emit('set-view', 'list')"
          title="List view (Ctrl+2)"
        ><List :size="14" :stroke-width="1.5" /></button>
      </div>

      <!-- Trash shortcut -->
      <button
        class="toolbar-btn ml-2"
        @click="$emit('show-trash')"
        title="Recycle bin"
      >
        <Trash2 :size="14" :stroke-width="1.5" />
        <span v-if="trashCount">{{ trashCount }}</span>
      </button>
    </template>

    <!-- Trash-mode actions. Order matters: the rightmost slot in
         browse-mode was the bin icon, so the rightmost slot here is
         the *back-to-workspace* arrow — clicking the same screen
         position you arrived from feels intuitive and avoids accidentally
         hitting Empty bin (a destructive action). The Empty bin button
         lives further left and uses neutral styling; the destructive
         intent is gated by the confirmation modal. -->
    <template v-else>
      <span class="text-2xs text-t3">
        {{ trashCount }} item{{ trashCount === 1 ? '' : 's' }}
      </span>
      <button
        class="toolbar-btn toolbar-btn--danger ml-2"
        :disabled="!trashCount || emptyingTrash"
        @click="$emit('empty-trash')"
        title="Permanently delete every item in the recycle bin"
      >{{ emptyingTrash ? 'Emptying…' : 'Empty bin' }}</button>
      <button
        class="toolbar-btn ml-2"
        @click="$emit('exit-trash')"
        title="Back to workspace"
      >
        <ArrowLeft class="w-3.5 h-3.5" :stroke-width="1.5" />
      </button>
    </template>
  </div>
</template>

<script setup>
import { ArrowLeft, ChevronDown, FolderPlus, FolderUp, LayoutGrid, List, Search, Trash2, Upload, X } from 'lucide-vue-next'
import { onMounted, onBeforeUnmount, ref } from 'vue'

defineProps({
  viewMode: { type: String, required: true },
  trashCount: { type: Number, default: 0 },
  search: { type: String, default: '' },
  viewingTrash: { type: Boolean, default: false },
  // While Empty bin is in flight (slow vector + KG + relational purge),
  // the parent flips this on; the button disables to prevent a double
  // fire and the label flips to communicate progress.
  emptyingTrash: { type: Boolean, default: false },
})
const emit = defineEmits([
  'new-folder', 'upload', 'upload-folder',
  'set-view', 'show-trash', 'update:search',
  'empty-trash', 'exit-trash',
])

// Upload-split popover. Click ▾ to open, click anywhere outside to
// close. Selecting an item closes the menu and emits the matching
// event up; the parent owns the actual file-input click.
const uploadMenuOpen = ref(false)
function onPickFiles() {
  uploadMenuOpen.value = false
  emit('upload')
}
function onPickFolder() {
  uploadMenuOpen.value = false
  emit('upload-folder')
}
function _onOutsideClick(e) {
  if (!uploadMenuOpen.value) return
  const t = e.target
  if (t?.closest && (t.closest('.upload-split'))) return
  uploadMenuOpen.value = false
}
onMounted(() => document.addEventListener('mousedown', _onOutsideClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', _onOutsideClick))
</script>

<style scoped>
.toolbar-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  font-size: 0.6875rem;
  color: var(--color-t2);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.toolbar-btn:hover:not(:disabled) {
  background: var(--color-bg2);
  color: var(--color-t1);
}
.toolbar-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* Upload split — main button + ▾ button glued together as one chip.
   The chev is its own button so clicks on each half do different
   things; visually they share the rounded outline. */
.upload-split {
  position: relative;
  display: inline-flex;
  align-items: stretch;
}
.upload-split__main {
  border-top-right-radius: 0;
  border-bottom-right-radius: 0;
  margin-right: 0;
}
.upload-split__chev {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 6px;
  font-size: 0.6875rem;
  color: var(--color-t3);
  background: transparent;
  border: 1px solid transparent;
  border-top-left-radius: 0;
  border-bottom-left-radius: 0;
  border-top-right-radius: 6px;
  border-bottom-right-radius: 6px;
  cursor: pointer;
  margin-left: -1px;
  transition: background 0.12s, color 0.12s;
}
.upload-split__chev:hover,
.upload-split--open .upload-split__chev {
  background: var(--color-bg2);
  color: var(--color-t1);
}
.upload-split__menu {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  z-index: 30;
  min-width: 220px;
  padding: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.upload-split__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 9px;
  font-size: 0.75rem;
  color: var(--color-t1);
  background: transparent;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  text-align: left;
}
.upload-split__item:hover { background: var(--color-bg-soft); }
.upload-split__title { font-weight: 500; }
.upload-split__desc {
  font-size: 0.625rem;
  color: var(--color-t3);
  margin-top: 2px;
}
.popup-enter-active, .popup-leave-active { transition: opacity .14s ease, transform .14s ease; }
.popup-enter-from, .popup-leave-to { opacity: 0; transform: translateY(-4px); }

/* Secondary-destructive variant — red text + visible neutral border
   so the button reads as a button at rest, with a red-tinted hover
   that surfaces the destructive intent only when the user reaches
   for it. Matches Vercel's outline-destructive pattern; the actual
   irreversible action is gated by the confirmation modal. */
.toolbar-btn--danger {
  color: var(--color-err-fg, #dc2626);
  border-color: var(--color-line);
}
.toolbar-btn--danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-err-fg, #dc2626) 10%, transparent);
  border-color: var(--color-err-fg, #dc2626);
  color: var(--color-err-fg, #dc2626);
}

.view-btn {
  width: 26px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 0.75rem;
  color: var(--color-t3);
  background: transparent;
  border-radius: 4px;
  border: none;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.view-btn:hover { color: var(--color-t1); background: var(--color-bg2); }
.view-btn--active { background: var(--color-bg3); color: var(--color-t1); }

/* Search field — inline icon + clear button */
.search-wrap {
  position: relative;
  display: flex;
  align-items: center;
  width: 240px;
}
.search-icon {
  position: absolute;
  left: 7px;
  width: 14px;
  height: 14px;
  color: var(--color-t3);
  pointer-events: none;
}
.search-input {
  width: 100%;
  /* 4px (not 5px) so the search input matches the 28px toolbar-btn
     height. With 5px padding the input renders 30px tall and pushes
     this whole bar 2px taller than Workspace's clone. */
  padding: 4px 26px 4px 24px;
  font-size: 0.6875rem;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  outline: none;
  transition: border-color 0.12s, box-shadow 0.12s;
}
.search-input:hover { border-color: var(--color-line2); }
.search-input:focus { border-color: var(--color-line2); box-shadow: var(--ring-focus); }
.search-input::placeholder { color: var(--color-t3); }
.search-clear {
  position: absolute;
  right: 4px;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 0.625rem;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 3px;
  cursor: pointer;
}
.search-clear:hover { background: var(--color-bg2); color: var(--color-t1); }
</style>
