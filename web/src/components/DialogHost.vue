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
          </div>
          <div class="dialog-actions">
            <button
              v-if="d.type === 'confirm'"
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

    <!-- Toast stack — fixed bottom-right, stacks upward -->
    <div class="toast-stack">
      <TransitionGroup name="toast">
        <div
          v-for="t in toasts"
          :key="t.id"
          :class="['toast', 'toast-' + t.variant]"
          @click="dismiss(t.id)"
          role="status"
        >
          <span class="toast-dot" />
          <span class="toast-msg">{{ t.message }}</span>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<script setup>
import { nextTick, ref, watch } from 'vue'
import { useDialog } from '@/composables/useDialog'

const { _dialogState: d, _toastList: toasts, _closeDialog, _dismissToast } = useDialog()
const dialogEl = ref(null)
const confirmBtn = ref(null)

function onConfirm() { _closeDialog(true) }
function onCancel() { _closeDialog(false) }
function dismiss(id) { _dismissToast(id) }

// Esc closes dialog (browser default doesn't fire on a div without focus)
watch(() => d.open, async (open) => {
  if (open) {
    await nextTick()
    dialogEl.value?.focus()
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
.dialog-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  padding: 12px 18px 14px;
  border-top: 1px solid var(--color-line);
}

/* Destructive variant — red filled button */
.btn-destructive {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  font-size: var(--fs-md);
  font-weight: 500;
  color: #fff;
  background: var(--color-err-fg);
  border: 1px solid var(--color-err-fg);
  border-radius: var(--r-sm);
  cursor: pointer;
  transition: background 0.12s, border-color 0.12s;
}
.btn-destructive:hover { filter: brightness(0.92); }

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
.toast {
  pointer-events: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 220px;
  max-width: 380px;
  padding: 9px 14px;
  font-size: 12px;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: var(--r-md);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.10);
  cursor: pointer;
}
.toast-dot {
  flex-shrink: 0;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-t3);
}
.toast-info    .toast-dot { background: var(--color-run-fg); }
.toast-success .toast-dot { background: var(--color-ok-fg); }
.toast-error   .toast-dot { background: var(--color-err-fg); }
.toast-warn    .toast-dot { background: var(--color-warn-fg); }

.toast-error   { border-color: color-mix(in srgb, var(--color-err-fg) 30%, var(--color-line)); }
.toast-success { border-color: color-mix(in srgb, var(--color-ok-fg) 30%, var(--color-line)); }
.toast-warn    { border-color: color-mix(in srgb, var(--color-warn-fg) 30%, var(--color-line)); }
.toast-msg { line-height: 1.4; }

.toast-enter-active, .toast-leave-active { transition: all 0.18s ease; }
.toast-enter-from { opacity: 0; transform: translateX(20px); }
.toast-leave-to   { opacity: 0; transform: translateX(20px); }
</style>
