<template>
  <Teleport to="body">
    <Transition name="preview-fade">
      <div
        v-if="open"
        class="preview-backdrop"
        @click.self="close"
      >
        <div
          class="preview-modal"
          :class="{ 'preview-modal--media': isMedia }"
          role="dialog"
          aria-modal="true"
          tabindex="-1"
          ref="modalEl"
        >
          <header class="preview-modal__header">
            <FileIcon
              kind="file"
              :name="filename"
              :size="14"
              class="preview-modal__icon"
            />
            <span class="preview-modal__name" :title="path">{{ filename }}</span>
            <span class="flex-1"></span>
            <a
              v-if="downloadUrl"
              class="preview-modal__btn"
              :href="downloadUrl"
              :title="t('workspace.download')"
            >
              <Download :size="14" :stroke-width="1.5" />
            </a>
            <button
              class="preview-modal__btn"
              @click="close"
              aria-label="Close"
              title="Close (Esc)"
            >
              <X :size="14" :stroke-width="1.75" />
            </button>
          </header>

          <div class="preview-modal__body">
            <ImagePreview
              v-if="kind === 'image'"
              :url="previewUrl"
              :filename="filename"
            />
            <VideoPreview
              v-else-if="kind === 'video'"
              :url="previewUrl"
              :filename="filename"
            />
            <AudioPreview
              v-else-if="kind === 'audio'"
              :url="previewUrl"
              :filename="filename"
            />
            <MarkdownPreview
              v-else-if="kind === 'markdown'"
              :url="previewUrl"
            />
            <PdfViewer
              v-else-if="kind === 'pdf'"
              :url="previewUrl"
              :download-url="downloadUrl"
            />
            <CodePreview
              v-else-if="kind === 'code'"
              :url="previewUrl"
              :filename="filename"
            />
            <SpreadsheetPreview
              v-else-if="kind === 'spreadsheet'"
              :url="previewUrl"
              :filename="filename"
            />
            <DocxPreview
              v-else-if="kind === 'docx'"
              :url="previewUrl"
            />
            <HtmlPreview
              v-else-if="kind === 'html'"
              :url="previewUrl"
            />
            <div v-else class="preview-modal__unsupported">
              <FileIcon
                kind="file"
                :name="filename"
                :size="40"
                class="preview-modal__unsupported-icon"
              />
              <div class="preview-modal__unsupported-title">
                {{ t('workspace.preview.unsupported_title') }}
              </div>
              <div class="preview-modal__unsupported-desc">
                {{ t('workspace.preview.unsupported_desc') }}
              </div>
              <a
                v-if="downloadUrl"
                class="preview-modal__unsupported-action"
                :href="downloadUrl"
              >
                <Download :size="13" :stroke-width="1.5" />
                <span>{{ t('workspace.download') }}</span>
              </a>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
/**
 * Workbench file preview modal.
 *
 * Centered overlay over the Workbench page (Library has its own
 * full-page DocDetail for indexed-document browsing — this is the
 * lighter "quick peek" peer for raw workdir files). Dispatches the
 * body to a per-kind viewer based on the filename's extension; any
 * extension we don't recognise renders a download fallback so the
 * user can still get at the content.
 *
 * Lifecycle:
 *   parent uses ``v-model:open`` to control visibility; ``path`` +
 *   ``filename`` describe the file the parent wants displayed.
 *   ``preview-url`` (mime-aware, inline disposition) feeds the
 *   image/video/audio elements; ``download-url`` (octet-stream,
 *   attachment) feeds the toolbar's save button.
 *
 * Keyboard:
 *   ESC anywhere on the page closes the modal (the backdrop also
 *   handles click-to-close). Future viewers will own their own
 *   keyboard shortcuts inside the body — the modal only owns ESC.
 */
import { computed, defineAsyncComponent, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Download, X } from 'lucide-vue-next'

import FileIcon from '@/components/workspace/FileIcon.vue'
import PdfViewer from '@/components/PdfViewer.vue'
import { previewKindFor } from './fileType'
import ImagePreview from './ImagePreview.vue'
import VideoPreview from './VideoPreview.vue'
import AudioPreview from './AudioPreview.vue'
import MarkdownPreview from './MarkdownPreview.vue'
// Heavier viewers — pulled in only when the user opens that kind
// of file. ``shiki`` (code), ``xlsx`` (spreadsheet), ``mammoth``
// (docx), ``dompurify`` (html) each add hundreds of KB; lazy-loading
// keeps them out of the main entry bundle for users who never
// preview those types.
const CodePreview = defineAsyncComponent(() => import('./CodePreview.vue'))
const SpreadsheetPreview = defineAsyncComponent(() => import('./SpreadsheetPreview.vue'))
const DocxPreview = defineAsyncComponent(() => import('./DocxPreview.vue'))
const HtmlPreview = defineAsyncComponent(() => import('./HtmlPreview.vue'))

const { t } = useI18n()

const props = defineProps({
  open: { type: Boolean, default: false },
  path: { type: String, default: '' },
  filename: { type: String, default: '' },
  // The parent owns URL construction so the same modal can serve
  // workdir / library / chat-attached / future agent-artifact
  // sources without us coupling to a single endpoint family.
  previewUrl: { type: String, default: '' },
  downloadUrl: { type: String, default: '' },
})
const emit = defineEmits(['update:open'])

const modalEl = ref(null)

const kind = computed(() => previewKindFor(props.filename))
// Wide canvas for any kind that benefits — visual media + long-form
// text. The 'unsupported' fallback stays compact (it's just a hint
// + download button; full bleed feels heavy).
const isMedia = computed(() => kind.value !== 'unsupported')

function close() {
  emit('update:open', false)
}

// ESC anywhere — global listener so the user doesn't have to focus
// the modal first. Bind only while open to avoid stealing keys
// from the rest of the page.
function onKey(e) {
  if (e.key === 'Escape') {
    e.stopPropagation()
    close()
  }
}
watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      window.addEventListener('keydown', onKey)
      nextTick(() => modalEl.value?.focus())
    } else {
      window.removeEventListener('keydown', onKey)
    }
  },
  { immediate: true },
)
</script>

<style scoped>
.preview-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(2px);
}
.preview-modal {
  display: flex;
  flex-direction: column;
  width: min(960px, 92vw);
  max-height: 88vh;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 10px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.4);
  overflow: hidden;
}
/* Media (image/video/pdf) needs a wider canvas — fill more of the
   viewport so the user can actually inspect a 4K screenshot or a
   landscape video without breaking out into "open in new tab". */
.preview-modal--media {
  width: min(1280px, 96vw);
  height: 88vh;
  max-height: 88vh;
}

.preview-modal__header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--color-line);
  background: var(--color-bg2);
  flex-shrink: 0;
}
.preview-modal__icon { color: var(--color-t3); flex-shrink: 0; }
.preview-modal__name {
  font-size: 12px;
  color: var(--color-t1);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.preview-modal__btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  color: var(--color-t2);
  background: transparent;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  text-decoration: none;
  transition: background 0.12s, color 0.12s;
}
.preview-modal__btn:hover {
  background: var(--color-bg3);
  color: var(--color-t1);
}

.preview-modal__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  position: relative;
  display: flex;
  background: var(--color-bg);
}

.preview-modal__unsupported {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 64px 32px;
  width: 100%;
  text-align: center;
}
.preview-modal__unsupported-icon { color: var(--color-t3); }
.preview-modal__unsupported-title {
  font-size: 13px;
  color: var(--color-t1);
}
.preview-modal__unsupported-desc {
  font-size: 11.5px;
  color: var(--color-t3);
  max-width: 360px;
  line-height: 1.5;
}
.preview-modal__unsupported-action {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  padding: 6px 12px;
  font-size: 11.5px;
  color: var(--color-t1);
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  text-decoration: none;
  transition: background 0.12s, border-color 0.12s;
}
.preview-modal__unsupported-action:hover {
  background: var(--color-bg3);
  border-color: var(--color-line2, var(--color-line));
}

.preview-fade-enter-active,
.preview-fade-leave-active {
  transition: opacity 0.18s ease;
}
.preview-fade-enter-active .preview-modal,
.preview-fade-leave-active .preview-modal {
  transition: transform 0.18s ease, opacity 0.18s ease;
}
.preview-fade-enter-from,
.preview-fade-leave-to { opacity: 0; }
.preview-fade-enter-from .preview-modal,
.preview-fade-leave-to .preview-modal {
  transform: translateY(8px);
  opacity: 0;
}
</style>
