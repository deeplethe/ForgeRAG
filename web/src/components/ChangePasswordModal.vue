<template>
  <div v-if="open" class="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
    <form @submit.prevent="onSubmit" class="w-80 p-5 rounded-lg bg-bg border border-line shadow-lg">
      <div class="text-[13px] text-t1 font-medium mb-1">
        {{ forced ? 'Set a new password' : 'Change password' }}
      </div>
      <div class="text-[10px] text-t3 mb-4 leading-relaxed">
        {{ forced
          ? 'You must change the default password before continuing.'
          : 'Choose a new password. This will sign out all your other sessions.' }}
      </div>

      <label v-if="!forced" class="block text-[10px] text-t3 mb-1">Current password</label>
      <input
        v-if="!forced"
        v-model="oldPassword" type="password" autocomplete="current-password"
        class="input mb-3"
      />

      <label class="block text-[10px] text-t3 mb-1">New password</label>
      <input
        v-model="newPassword" type="password" autocomplete="new-password"
        class="input mb-3"
      />

      <label class="block text-[10px] text-t3 mb-1">Confirm new password</label>
      <input
        v-model="confirmPassword" type="password" autocomplete="new-password"
        class="input mb-4"
      />

      <div class="flex items-center justify-end gap-2">
        <button v-if="!forced" type="button" @click="$emit('close')"
          class="px-3 py-1.5 text-[11px] text-t2 hover:text-t1">Cancel</button>
        <button type="submit" :disabled="loading || !valid"
          class="px-3 py-1.5 rounded-md bg-t1 text-white text-[11px] disabled:opacity-40">
          {{ loading ? 'Saving…' : 'Update password' }}
        </button>
      </div>

      <div v-if="error" class="mt-3 text-[10px]" style="color: var(--color-err-fg);">{{ error }}</div>
    </form>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
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

const valid = computed(() =>
  newPassword.value.length >= 4 && newPassword.value === confirmPassword.value,
)

watch(() => props.open, (v) => {
  if (v) {
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    error.value = ''
  }
})

async function onSubmit() {
  if (!valid.value) {
    error.value = newPassword.value.length < 4
      ? 'New password must be at least 4 characters.'
      : 'Passwords don\'t match.'
    return
  }
  error.value = ''
  loading.value = true
  try {
    await changePassword(oldPassword.value, newPassword.value)
    emit('changed')
    emit('close')
  } catch (e) {
    error.value = e.message || 'Change failed.'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* .input uses the global definition from style.css. */
</style>
