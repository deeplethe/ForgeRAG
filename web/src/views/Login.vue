<template>
  <div class="auth-shell">
    <!-- Hide the form while we probe ``/auth/me`` to decide whether
         auth is actually required; otherwise the form flashes for a
         tick before the redirect when auth is disabled. -->
    <form v-if="!probing" @submit.prevent="onSubmit" class="auth-card" novalidate>
      <div class="auth-brand">
        <img src="/craig.png" alt="" class="auth-logo" />
        <span class="wordmark text-xl">OpenCraig</span>
        <span class="auth-subtitle">Sign in to continue</span>
      </div>

      <label class="auth-label">Email</label>
      <input
        v-model="email" ref="userInput" type="email" autocomplete="email"
        placeholder="you@example.com"
        :class="['input', 'mb-3', { 'input--err': fieldErr === 'email' }]"
        @input="onFieldInput"
      />

      <label class="auth-label">Password</label>
      <input
        v-model="password" type="password" autocomplete="current-password"
        :class="['input', 'mb-4', { 'input--err': fieldErr === 'password' }]"
        @input="onFieldInput"
      />

      <button
        type="submit"
        :disabled="loading"
        class="auth-submit"
      >{{ loading ? 'Signing in…' : 'Sign in' }}</button>

      <div v-if="error" class="auth-error">{{ error }}</div>

      <div class="auth-foot">
        New here?
        <router-link to="/register" class="auth-link">Create an account</router-link>
      </div>
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
const fieldErr = ref('')             // 'email' | 'password' | '' — drives input ring
const userInput = ref(null)
const probing = ref(true)            // true until we know whether to show the form

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

function onFieldInput() {
  // Clear the error+ring as soon as the user starts correcting,
  // so they don't keep staring at a stale red message.
  if (error.value) error.value = ''
  if (fieldErr.value) fieldErr.value = ''
}

async function onSubmit() {
  error.value = ''
  fieldErr.value = ''

  // Client-side validation — saves a server round-trip and avoids
  // raw "400: missing email" leaking into the UI.
  const emailVal = email.value.trim()
  if (!emailVal) {
    error.value = 'Enter your email to sign in.'
    fieldErr.value = 'email'
    return
  }
  if (!password.value) {
    error.value = 'Enter your password.'
    fieldErr.value = 'password'
    return
  }

  loading.value = true
  try {
    const r = await apiLogin(emailVal, password.value)
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
    // Map status codes to human-readable copy. Falls through to a
    // generic message rather than echoing back "400: <server detail>"
    // — operators read server logs, end users don't.
    const status = e?.status
    const detail = (e?.message || '').toLowerCase()
    if (status === 401) {
      error.value = 'Incorrect email or password.'
    } else if (status === 403 && detail.includes('pending')) {
      error.value = 'Your account is pending admin approval.'
    } else if (status === 403 && detail.includes('suspended')) {
      error.value = 'This account has been suspended. Contact your administrator.'
    } else if (status === 403) {
      error.value = "Sign-in is blocked for this account."
    } else if (status === 429) {
      error.value = 'Too many attempts. Please wait a moment and try again.'
    } else if (status >= 500) {
      error.value = "The server hit an error. Try again in a moment."
    } else if (!status) {
      error.value = "Couldn't reach the server. Check your connection."
    } else {
      error.value = 'Sign-in failed. Please try again.'
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* ── Shell ─────────────────────────────────────────────────────── */
.auth-shell {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  background: var(--color-bg2);
}

/* ── Card ──────────────────────────────────────────────────────── */
.auth-card {
  width: 100%;
  max-width: 360px;
  padding: 28px 28px 22px;
  border: 1px solid var(--color-line);
  border-radius: 12px;
  background: var(--color-bg);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
}

.auth-brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  margin-bottom: 22px;
}
.auth-logo {
  width: 44px;
  height: 44px;
  border-radius: 999px;
}
.auth-subtitle {
  margin-top: 2px;
  font-size: 0.6875rem;
  color: var(--color-t3);
}

/* ── Inputs ────────────────────────────────────────────────────── */
.auth-label {
  display: block;
  font-size: 0.6875rem;
  color: var(--color-t3);
  margin-bottom: 4px;
}
.input--err {
  border-color: var(--color-err-fg, #d23) !important;
  box-shadow: 0 0 0 1px var(--color-err-fg, #d23) inset;
}

/* ── Submit ────────────────────────────────────────────────────── */
.auth-submit {
  width: 100%;
  padding: 9px 12px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: var(--color-t1);
  color: var(--color-bg);
  font-size: 0.75rem;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.12s ease, transform 0.06s ease;
}
.auth-submit:hover:not(:disabled) { opacity: 0.92; }
.auth-submit:active:not(:disabled) { transform: translateY(1px); }
.auth-submit:disabled { opacity: 0.55; cursor: not-allowed; }

/* ── Error + footer ────────────────────────────────────────────── */
.auth-error {
  margin-top: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 0.6875rem;
  line-height: 1.45;
  color: var(--color-err-fg, #d23);
  background: var(--color-err-bg, rgba(214, 60, 50, 0.08));
  border: 1px solid var(--color-err-line, rgba(214, 60, 50, 0.25));
}
.auth-foot {
  margin-top: 18px;
  text-align: center;
  font-size: 0.6875rem;
  color: var(--color-t3);
}
.auth-link {
  color: var(--color-t1);
  text-decoration: none;
  border-bottom: 1px solid var(--color-line);
  padding-bottom: 1px;
  transition: border-color 0.12s ease;
}
.auth-link:hover { border-bottom-color: var(--color-t1); }
</style>
