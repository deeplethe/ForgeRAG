<template>
  <div
    class="att-chip"
    :class="{ 'att-chip--clickable': !!attachment.attachment_id }"
    :title="tooltip"
    @click="open"
  >
    <span class="att-chip__icon" :class="'att-chip__icon--' + iconKind">
      <component :is="iconComponent" :size="16" :stroke-width="1.5" />
    </span>
    <div class="att-chip__body">
      <div class="att-chip__name">{{ attachment.filename }}</div>
      <div class="att-chip__meta">{{ metaLine }}</div>
    </div>
    <button
      v-if="removable"
      type="button"
      class="att-chip__remove"
      :title="t('chat.attachments.remove')"
      @click.stop="$emit('remove', attachment)"
    >
      <X :size="12" :stroke-width="1.75" />
    </button>
  </div>
</template>

<script setup>
/**
 * One attachment chip in the chat composer's pre-send rail (or in
 * the rendered message body, post-send). Visual is a small rounded
 * row: [icon] [name + meta] [×?].
 *
 * Pulls icon + colour from the attachment's ``kind`` (``text`` /
 * ``image`` / ``pdf`` / ``other``) — drives a different glyph and
 * a different lift colour so the user can scan a row of chips
 * without reading filenames. Click opens the blob (preview / new
 * tab); the × emits ``remove`` so the parent can DELETE the row.
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { FileText, Image as ImageIcon, FileType, Paperclip, X } from 'lucide-vue-next'
import { attachmentBlobUrl } from '@/api'

const props = defineProps({
  attachment: { type: Object, required: true },
  removable: { type: Boolean, default: true },
})
defineEmits(['remove'])

const { t } = useI18n()

// kind → (lucide component, semantic class for icon tint)
const _ICONS = {
  text:  { icon: FileText,   tint: 'text' },
  image: { icon: ImageIcon,  tint: 'image' },
  pdf:   { icon: FileType,   tint: 'pdf' },
  other: { icon: Paperclip,  tint: 'other' },
}

const iconKind = computed(() => _ICONS[props.attachment.kind]?.tint || 'other')
const iconComponent = computed(() => _ICONS[props.attachment.kind]?.icon || Paperclip)

const metaLine = computed(() => {
  const k = props.attachment.kind
  const size = fmtSize(props.attachment.size_bytes)
  const label =
    k === 'text' ? 'TEXT' :
    k === 'image' ? 'IMAGE' :
    k === 'pdf' ? 'PDF' :
    (props.attachment.mime || 'FILE').toUpperCase().slice(0, 16)
  return `${label} · ${size}`
})

const tooltip = computed(() => {
  const a = props.attachment
  return `${a.filename}\n${a.mime} · ${fmtSize(a.size_bytes)}`
})

function fmtSize(n) {
  if (!n && n !== 0) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function open() {
  if (!props.attachment.attachment_id) return
  // New tab — keeps the chat conversation open and lets the
  // browser pick the right viewer (PDF / image / text). The blob
  // endpoint serves with ``Content-Disposition: inline``.
  window.open(attachmentBlobUrl(props.attachment.attachment_id), '_blank', 'noopener')
}
</script>

<style scoped>
.att-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px 6px 6px;
  max-width: 240px;
  border: 1px solid var(--color-line);
  border-radius: 8px;
  background: var(--color-bg);
  font-size: 0.75rem;
  line-height: 1.2;
  transition: border-color .12s, background-color .12s;
}
.att-chip--clickable { cursor: pointer; }
.att-chip--clickable:hover {
  border-color: var(--color-line2);
  background: var(--color-bg2);
}

.att-chip__icon {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: var(--color-bg3);
}
/* kind-specific tints — subtle, just enough to scan a row of
   chips without reading the names. */
.att-chip__icon--text  { color: var(--color-t2); }
.att-chip__icon--image { color: #2563eb; background: color-mix(in srgb, #2563eb 12%, var(--color-bg3)); }
.att-chip__icon--pdf   { color: #b91c1c; background: color-mix(in srgb, #b91c1c 12%, var(--color-bg3)); }
.att-chip__icon--other { color: var(--color-t3); }

.att-chip__body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.att-chip__name {
  color: var(--color-t1);
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.att-chip__meta {
  color: var(--color-t3);
  font-size: 0.625rem;
  font-feature-settings: "tnum";
  letter-spacing: 0.02em;
  white-space: nowrap;
}

.att-chip__remove {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-left: 2px;
  padding: 0;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
.att-chip__remove:hover {
  color: var(--color-t1);
  background: var(--color-bg3);
}
</style>
