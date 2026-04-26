<template>
  <div class="flex items-center justify-center min-h-screen bg-bg2">
    <form @submit.prevent="onSubmit" class="w-80 p-6 rounded-lg border border-line bg-bg shadow-sm">
      <div class="flex justify-center mb-6">
        <span class="wordmark text-[20px]">ForgeRAG</span>
      </div>

      <label class="block text-[11px] text-t3 mb-1">Username</label>
      <input
        v-model="username" ref="userInput" autocomplete="username"
        class="input mb-3"
      />

      <label class="block text-[11px] text-t3 mb-1">Password</label>
      <input
        v-model="password" type="password" autocomplete="current-password"
        class="input mb-4"
      />

      <button
        type="submit"
        :disabled="loading"
        class="w-full py-2 rounded-md bg-t1 text-white text-[12px] disabled:opacity-50"
      >{{ loading ? 'Signing in…' : 'Sign in' }}</button>

      <div v-if="error" class="mt-3 text-[11px]" style="color: var(--color-err-fg);">{{ error }}</div>
    </form>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { login as apiLogin } from '@/api/auth'

const router = useRouter()
const route = useRoute()

const username = ref('admin')    // single-user default; multi-user ready
const password = ref('')
const loading = ref(false)
const error = ref('')
const userInput = ref(null)

onMounted(() => {
  // Focus password if username is prefilled (common single-user case),
  // else focus username.
  if (username.value) {
    document.querySelector('input[type=password]')?.focus()
  } else {
    userInput.value?.focus()
  }
})

async function onSubmit() {
  error.value = ''
  loading.value = true
  try {
    const r = await apiLogin(username.value, password.value)
    // Redirect: honour ?redirect=, else go to /chat
    const dest = route.query.redirect || '/chat'
    // Force a hard reload so every cached request picks up new cookie.
    // Client-side router.push works too, but this is more reliable in
    // presence of SSE connections and pinia/composable caches.
    window.location.href = dest
    // If must_change_password, the router guard on the destination will
    // open the ChangePassword modal via the /auth/me probe.
    void r
  } catch (e) {
    error.value = e.message?.includes('401')
      ? 'Incorrect username or password.'
      : e.message || 'Login failed.'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* Login form uses the global .input from style.css.
   The Sign-in button uses `bg-t1 text-white` which now resolves to the
   Vercel near-black via the updated token value. */
</style>
