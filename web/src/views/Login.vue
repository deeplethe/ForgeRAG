<template>
  <div class="flex items-center justify-center min-h-screen bg-bg2">
    <!-- Hide the form while we probe ``/auth/me`` to decide whether
         auth is actually required; otherwise the form flashes for a
         tick before the redirect when auth is disabled. -->
    <form v-if="!probing" @submit.prevent="onSubmit" class="w-80 p-6 rounded-lg border border-line bg-bg shadow-sm">
      <div class="flex flex-col items-center mb-6 gap-2">
        <img src="/craig.png" alt="" class="w-12 h-12 rounded-full" />
        <span class="wordmark text-[20px]">OpenCraig</span>
      </div>

      <label class="block text-[11px] text-t3 mb-1">Email</label>
      <input
        v-model="email" ref="userInput" type="email" autocomplete="email"
        placeholder="you@example.com"
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
import { login as apiLogin, getMe } from '@/api/auth'

const router = useRouter()
const route = useRoute()

// Email is now the primary login identifier. Empty default —
// users always type their address. Legacy bootstrap admins whose
// email column is NULL can still sign in by entering their
// username in this field; the backend's login route falls back
// to a username-column lookup when no email match is found.
const email = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')
const userInput = ref(null)
const probing = ref(true)        // true until we know whether to show the form

onMounted(async () => {
  // If auth is disabled on the server (or the user is already logged in),
  // ``/auth/me`` succeeds — in that case skip the login form entirely
  // and forward the user to the destination. Only show the form when
  // we know auth is actually required.
  try {
    const me = await getMe()
    if (me) {
      const dest = route.query.redirect || '/chat'
      window.location.href = dest
      return
    }
  } catch {
    // 401 → user is genuinely logged out, fall through to show form.
  }
  probing.value = false
  // Always focus the email input on mount — there's no
  // pre-fill anymore (single-user "admin" default went away
  // when login switched to email).
  userInput.value?.focus()
})

async function onSubmit() {
  error.value = ''
  loading.value = true
  try {
    const r = await apiLogin(email.value.trim(), password.value)
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
      ? 'Incorrect email or password.'
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
