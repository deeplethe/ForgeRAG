<template>
  <Transition name="modal-fade">
    <div v-if="open" class="modal-shell" @mousedown.self="onBackdropClick">
      <form @submit.prevent="onSubmit" class="modal-card" novalidate>
        <div class="modal-title">
          {{ forced ? 'Set a new password' : 'Change password' }}
        </div>
        <div class="modal-subtitle">
          {{ forced
            ? 'You must change the default password before continuing.'
            : 'Choose a new password. This will sign out all your other sessions.' }}
        </div>

        <template v-if="!forced">
          <label class="modal-label">Current password</label>
          <input
            v-model="oldPassword" type="password" autocomplete="current-password"
            :class="['input', 'mb-3', { 'input--err': fieldErr === 'old' }]"
            @input="onFieldInput"
          />
        </template>

        <label class="modal-label">New password</label>
        <input
          v-model="newPassword" type="password" autocomplete="new-password"
          ref="newInput"
          placeholder="At least 8 characters"
          :class="['input', 'mb-3', { 'input--err': fieldErr === 'new' }]"
          @input="onFieldInput"
        />

        <label class="modal-label">Confirm new password</label>
        <input
          v-model="confirmPassword" type="password" autocomplete="new-password"
          :class="['input', 'mb-4', { 'input--err': fieldErr === 'confirm' }]"
          @input="onFieldInput"
          @keyup.enter="onSubmit"
        />

        <div class="modal-actions">
          <button v-if="!forced" type="button" @click="$emit('close')"
            class="modal-btn modal-btn--ghost">Cancel</button>
          <button type="submit" :disabled="loading"
            class="modal-btn modal-btn--primary">
            {{ loading ? 'Saving…' : 'Update password' }}
          </button>
        </div>

        <div v-if="error" class="modal-error">{{ error }}</div>
      </form>
    </div>
  </Transition>
</template>

<script setup>
import { nextTick, ref, watch } from 'vue'
import { changePassword } from '@/api/auth'

const props = defineProps({
  open: { type: Boolean, default: false },
  forced: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'changed'])

const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const loading = ref(false)
const error = ref('')
const fieldErr = ref('')   // 'old' | 'new' | 'confirm' | ''
const newInput = ref(null)

watch(() => props.open, async (v) => {
  if (v) {
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    error.value = ''
    fieldErr.value = ''
    await nextTick()
    // Forced flow: focus the new-password field directly. Voluntary
    // flow: focus the current-password field (rendered first).
    newInput.value?.focus()
  }
})

function onFieldInput() {
  if (error.value) error.value = ''
  if (fieldErr.value) fieldErr.value = ''
}

function onBackdropClick() {
  // Forced flow: backdrop is unclickable — user MUST set a new
  // password. Voluntary flow: clicking outside dismisses.
  if (!props.forced) emit('close')
}

async function onSubmit() {
  error.value = ''
  fieldErr.value = ''

  if (!props.forced && !oldPassword.value) {
    error.value = 'Enter your current password.'
    fieldErr.value = 'old'
    return
  }
  if (!newPassword.value) {
    error.value = 'Choose a new password.'
    fieldErr.value = 'new'
    return
  }
  if (newPassword.value.length < 8) {
    error.value = 'New password must be at least 8 characters.'
    fieldErr.value = 'new'
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = "Passwords don't match."
    fieldErr.value = 'confirm'
    return
  }
  if (!props.forced && newPassword.value === oldPassword.value) {
    error.value = "New password can't be the same as the current one."
    fieldErr.value = 'new'
    return
  }

  loading.value = true
  try {
    await changePassword(oldPassword.value, newPassword.value)
    emit('changed')
    emit('close')
  } catch (e) {
    const status = e?.status
    const detail = (e?.message || '').toLowerCase()
    if (status === 401 || (status === 400 && detail.includes('current'))) {
      error.value = 'Your current password is incorrect.'
      fieldErr.value = 'old'
    } else if (status === 400 && detail.includes('weak')) {
      error.value = 'That password is too weak — try something longer.'
      fieldErr.value = 'new'
    } else if (status === 400) {
      error.value = "Something in the form looks off — please double-check."
    } else if (status >= 500) {
      error.value = 'The server hit an error. Try again in a moment.'
    } else if (!status) {
      error.value = "Couldn't reach the server. Check your connection."
    } else {
      error.value = "Couldn't update your password. Please try again."
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* ── Backdrop — opaque enough that the page behind is gone ─────── */
.modal-shell {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  background: rgba(0, 0, 0, 0.72);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}

/* ── Card — same visual language as Login.vue / Register.vue ────── */
.modal-card {
  width: 100%;
  max-width: 360px;
  padding: 22px 22px 18px;
  border: 1px solid var(--color-line);
  border-radius: 12px;
  background: var(--color-bg);
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.35);
}
.modal-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-t1);
  margin-bottom: 4px;
}
.modal-subtitle {
  font-size: 11px;
  color: var(--color-t3);
  line-height: 1.5;
  margin-bottom: 16px;
}
.modal-label {
  display: block;
  font-size: 11px;
  color: var(--color-t3);
  margin-bottom: 4px;
}
.input--err {
  border-color: var(--color-err-fg, #d23) !important;
  box-shadow: 0 0 0 1px var(--color-err-fg, #d23) inset;
}

/* ── Buttons ───────────────────────────────────────────────────── */
.modal-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
}
.modal-btn {
  padding: 7px 14px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid transparent;
  transition: opacity 0.12s ease, background 0.12s ease, color 0.12s ease;
}
.modal-btn--ghost {
  background: transparent;
  color: var(--color-t2);
}
.modal-btn--ghost:hover { color: var(--color-t1); background: var(--color-bg2); }
/* Primary uses the inverse pairing: light-bg on dark theme, dark-bg
   on light theme — same as `auth-submit` in Login / Register. The
   old `bg-t1 text-white` rendered as light-on-light in the dark
   theme. */
.modal-btn--primary {
  background: var(--color-t1);
  color: var(--color-bg);
}
.modal-btn--primary:hover:not(:disabled) { opacity: 0.92; }
.modal-btn--primary:active:not(:disabled) { transform: translateY(1px); }
.modal-btn--primary:disabled { opacity: 0.55; cursor: not-allowed; }

/* ── Error banner ──────────────────────────────────────────────── */
.modal-error {
  margin-top: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.45;
  color: var(--color-err-fg, #d23);
  background: var(--color-err-bg, rgba(214, 60, 50, 0.08));
  border: 1px solid var(--color-err-line, rgba(214, 60, 50, 0.25));
}

/* ── Enter / leave transition ──────────────────────────────────── */
.modal-fade-enter-active, .modal-fade-leave-active {
  transition: opacity 0.15s ease;
}
.modal-fade-enter-active .modal-card,
.modal-fade-leave-active .modal-card {
  transition: transform 0.15s ease, opacity 0.15s ease;
}
.modal-fade-enter-from, .modal-fade-leave-to { opacity: 0; }
.modal-fade-enter-from .modal-card,
.modal-fade-leave-to .modal-card { transform: translateY(6px); opacity: 0; }
</style>
