<template>
  <!-- Dialog backdrop + modal. Single instance app-wide. -->
  <Teleport to="body">
    <Transition name="dialog">
      <div
        v-if="d.open"
        class="dialog-backdrop"
        @click.self="onCancel"
        @keydown.esc="onCancel"
      >
        <div class="dialog panel" role="dialog" aria-modal="true" tabindex="-1" ref="dialogEl">
          <div class="dialog-body">
            <h2 v-if="d.title" class="dialog-title">{{ d.title }}</h2>
            <p v-if="d.description" class="dialog-desc">{{ d.description }}</p>
            <!-- Prompt-only: text input. Auto-focused + selected
                 on open (see watcher below) so the user can
                 just start typing. Enter submits, Esc cancels —
                 same keyboard contract as confirm/alert. -->
            <input
              v-if="d.type === 'prompt'"
              ref="inputEl"
              v-model="d.inputValue"
              type="text"
              class="dialog-input"
              :placeholder="d.inputPlaceholder"
              @keydown.enter.prevent="onConfirm"
              @keydown.esc.prevent="onCancel"
            />
          </div>
          <div class="dialog-actions">
            <button
              v-if="d.type === 'confirm' || d.type === 'prompt'"
              class="btn-secondary"
              @click="onCancel"
            >{{ d.cancelText }}</button>
            <button
              :class="d.variant === 'destructive' ? 'btn-destructive' : 'btn-primary'"
              @click="onConfirm"
              ref="confirmBtn"
            >{{ d.confirmText }}</button>
          </div>
        </div>
      </div>
    </Transition>

    <!-- Toast stack — fixed bottom-right, stacks upward. Each row is
         its own component so the visual + interaction logic is reusable
         and easy to swap when the design system evolves. -->
    <div class="toast-stack">
      <TransitionGroup name="toast">
        <ToastItem
          v-for="t in toasts"
          :key="t.id"
          :item="t"
          @action="onAction(t)"
          @dismiss="dismiss(t.id)"
        />
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<script setup>
import { nextTick, ref, watch } from 'vue'
import { useDialog } from '@/composables/useDialog'
import ToastItem from './ToastItem.vue'

const { _dialogState: d, _toastList: toasts, _closeDialog, _dismissToast } = useDialog()
const dialogEl = ref(null)
const confirmBtn = ref(null)
const inputEl = ref(null)

function onConfirm() {
  // Prompt resolves with the trimmed input value, or null when
  // empty (treated as "no change" by the caller). Confirm /
  // alert keep their boolean contract.
  if (d.type === 'prompt') {
    const val = (d.inputValue || '').trim()
    _closeDialog(val || null)
  } else {
    _closeDialog(true)
  }
}
function onCancel() {
  // Prompt resolves null on cancel (so callers can if (val == null) skip).
  // Confirm resolves false. Same caller-side null/false discrimination
  // either way.
  _closeDialog(d.type === 'prompt' ? null : false)
}
function dismiss(id) { _dismissToast(id) }
function onAction(t) {
  // Fire the action then immediately dismiss — the user has answered
  // the prompt, the toast no longer needs to claim screen space.
  try { t.action?.onClick?.() } finally { _dismissToast(t.id) }
}

// Esc closes dialog (browser default doesn't fire on a div without focus)
watch(() => d.open, async (open) => {
  if (open) {
    await nextTick()
    // For prompts, focus + select the text input so the user
    // can start typing or replace the existing value with one
    // keystroke. For confirm/alert, focus the dialog body so
    // Esc still works (the @keydown handler on the backdrop
    // doesn't fire if no descendant has focus).
    if (d.type === 'prompt' && inputEl.value) {
      inputEl.value.focus()
      inputEl.value.select?.()
    } else {
      dialogEl.value?.focus()
    }
  }
})
function onKey(e) { if (d.open && e.key === 'Escape') onCancel() }
window.addEventListener('keydown', onKey)
</script>

<style scoped>
/* ── Backdrop ───────────────────────────────────────────────────── */
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

/* ── Dialog card ────────────────────────────────────────────────── */
.dialog {
  width: 100%;
  max-width: 420px;
  background: var(--color-bg);
  outline: none;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}
.dialog-body {
  padding: 20px 22px 16px;
}
.dialog-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-t1);
  letter-spacing: -0.01em;
}
.dialog-desc {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-t2);
}

/* Prompt-type dialogs only — text input matches the ``.input``
   pattern Profile / Search / Tokens already use (32px tall,
   subtle border, focus ring via the design system's
   ``--ring-focus`` variable). Keeps the Settings-page input
   look consistent across modal + inline contexts. */
.dialog-input {
  margin-top: 12px;
  width: 100%;
  height: 32px;
  padding: 0 10px;
  font-size: 13px;
  border: 1px solid var(--color-line);
  border-radius: var(--r-sm);
  background: var(--color-bg);
  color: var(--color-t1);
  outline: none;
}
.dialog-input:focus {
  border-color: var(--color-line2);
  box-shadow: var(--ring-focus);
}
.dialog-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  padding: 12px 18px 14px;
  border-top: 1px solid var(--color-line);
}

/* Destructive variant — solid red, white text. ``filter:
   brightness`` was a Material-era hack and made the button look
   muddy on dark themes; an explicit darker-red on hover is cleaner
   and matches Vercel's destructive-button pattern. */
.btn-destructive {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  font-size: var(--fs-md);
  font-weight: 500;
  color: #fff;
  background: #dc2626;            /* red-600 — single-source for both themes */
  border: 1px solid #dc2626;
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.btn-destructive:hover {
  background: #b91c1c;            /* red-700 */
  border-color: #b91c1c;
}

/* ── Dialog enter/leave ─────────────────────────────────────────── */
.dialog-enter-active, .dialog-leave-active {
  transition: opacity 0.15s ease;
}
.dialog-enter-active .dialog, .dialog-leave-active .dialog {
  transition: transform 0.18s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.15s;
}
.dialog-enter-from, .dialog-leave-to { opacity: 0; }
.dialog-enter-from .dialog, .dialog-leave-to .dialog {
  transform: translateY(8px) scale(0.98);
  opacity: 0;
}

/* ── Toast stack ────────────────────────────────────────────────── */
/* Layout-only — each row is rendered by <ToastItem> with its own
   self-contained styles. */
.toast-stack {
  position: fixed;
  bottom: 36px;        /* above the upload bar (26px) + a 10px gap */
  right: 20px;
  z-index: 99;
  display: flex;
  flex-direction: column-reverse;  /* newest on top */
  gap: 8px;
  pointer-events: none;
}
.toast-enter-active, .toast-leave-active { transition: all 0.18s ease; }
.toast-enter-from { opacity: 0; transform: translateX(20px); }
.toast-leave-to   { opacity: 0; transform: translateX(20px); }
</style>
