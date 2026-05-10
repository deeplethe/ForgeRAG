<template>
  <!--
    One toast row. Stateless presentation; the lifecycle (push, auto-dismiss
    after TTL) lives in ``useDialog``. Vercel-style: tight padding, single
    line of text, action as a borderless link-button on the right, optional
    dismiss `×` to close immediately.

    Props:
      item: { id, message, variant, action? }
        variant: 'info' | 'success' | 'error' | 'warn'
        action?: { label: string, onClick: () => void }

    Emits:
      action — fired when the action button is clicked. Parent
               (DialogHost) runs ``item.action.onClick`` and dismisses.
      dismiss — fired by the explicit `×` button.
  -->
  <div :class="['toast', `toast--${item.variant}`]" role="status">
    <component :is="iconFor(item.variant)" class="toast__icon" :size="16" :stroke-width="1.5" aria-hidden="true" />
    <span class="toast__msg">{{ item.message }}</span>
    <button
      v-if="item.action"
      class="toast__action"
      @click.stop="$emit('action')"
    >{{ item.action.label }}</button>
    <!-- Loading toasts represent an in-flight operation; manually
         dismissing them would imply cancellation, which is misleading.
         Caller is responsible for closing it via ``dismissToast(id)``. -->
    <button
      v-if="item.variant !== 'loading'"
      class="toast__dismiss"
      aria-label="Dismiss"
      @click.stop="$emit('dismiss')"
    >
      <X class="toast__dismiss-icon" :size="12" :stroke-width="1.5" />
    </button>
  </div>
</template>

<script setup>
// Icons from Lucide (chosen for the Vercel/Geist visual fit). Default
// stroke is 2px; passing 1.5 throughout the toast keeps lines from
// reading thicker than the body text at 12px.
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle,
  Info,
  Loader2,
  X,
} from 'lucide-vue-next'

const ICON_MAP = {
  success: CheckCircle,
  error: AlertCircle,
  warn: AlertTriangle,
  info: Info,
  // Loader2 ships with built-in spin on Lucide; we still use the CSS
  // animation so the timing matches the rest of the chrome (and so
  // there's no visual glitch on browsers that don't run SMIL).
  loading: Loader2,
}

function iconFor(variant) {
  return ICON_MAP[variant] || Info
}

defineProps({
  item: { type: Object, required: true },
})
defineEmits(['action', 'dismiss'])
</script>

<style scoped>
/* Vercel-style toast:
   - Solid surface (not translucent — readable over any backdrop)
   - 1px border in the same hairline color the rest of the app uses
   - Layered shadow: a tight contact shadow + a soft ambient one
   - 12px text, line-height 1.5
   - Variant dot is a small inline icon (not a colored disc) — colour
     comes from the icon stroke, the surrounding chrome stays neutral. */
.toast {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 260px;
  max-width: 380px;
  padding: 8px 8px 8px 12px;
  font-size: 0.75rem;
  line-height: 1.5;
  color: var(--color-t1);
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 8px;
  box-shadow:
    0 1px 2px rgba(0, 0, 0, 0.06),
    0 4px 16px rgba(0, 0, 0, 0.08);
  pointer-events: auto;
}

.toast__icon {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  color: var(--color-t3);   /* neutral default; variants override */
}
.toast--success .toast__icon { color: var(--color-ok-fg, #10b981); }
.toast--error   .toast__icon { color: var(--color-err-fg, #dc2626); }
.toast--warn    .toast__icon { color: var(--color-warn-fg, #d97706); }
.toast--info    .toast__icon { color: var(--color-run-fg, #2563eb); }
.toast--loading .toast__icon {
  color: var(--color-run-fg, #2563eb);
  animation: toast-spin 0.9s linear infinite;
}
@keyframes toast-spin {
  from { transform: rotate(0deg); }
  to   { transform: rotate(360deg); }
}

.toast__msg {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Action — text-link styling, the primary affordance the user is
   meant to consider. Bolder weight + underline-on-hover. No border so
   it doesn't compete with the dismiss `×`. */
.toast__action {
  flex-shrink: 0;
  padding: 4px 8px;
  font-size: 0.75rem;
  font-weight: 500;
  color: var(--color-t1);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.12s;
}
.toast__action:hover {
  background: var(--color-bg2);
  text-decoration: underline;
  text-underline-offset: 2px;
}

/* Dismiss — secondary, low-weight, rotates in to a hover tint. */
.toast__dismiss {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  color: var(--color-t3);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.toast__dismiss:hover {
  color: var(--color-t1);
  background: var(--color-bg2);
}
.toast__dismiss-icon {
  width: 12px;
  height: 12px;
}
</style>
