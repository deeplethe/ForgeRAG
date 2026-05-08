<template>
  <div class="auth-shell">
    <form v-if="!probing" @submit.prevent="onSubmit" class="auth-card" novalidate>
      <div class="auth-brand">
        <img src="/craig.png" alt="" class="auth-logo" />
        <span class="wordmark text-[20px]">OpenCraig</span>
        <span class="auth-subtitle">Create your account</span>
      </div>

      <!-- Success state — registration succeeded but the user might be
           in pending_approval mode. We don't auto-login (the backend
           doesn't either), so route them back to login with the right
           message based on returned status. -->
      <div v-if="successMessage" class="auth-success">
        {{ successMessage }}
        <div class="auth-foot" style="margin-top: 14px">
          <router-link to="/login" class="auth-link">Back to sign in</router-link>
        </div>
      </div>

      <template v-else>
        <label class="auth-label">Email</label>
        <input
          v-model="email" ref="emailInput" type="email" autocomplete="email"
          placeholder="you@example.com"
          :class="['input', 'mb-3', { 'input--err': fieldErr === 'email' }]"
          @input="onFieldInput"
        />

        <label class="auth-label">Username</label>
        <input
          v-model="username" type="text" autocomplete="username"
          maxlength="32"
          placeholder="craig"
          :class="['input', { 'input--err': fieldErr === 'username' }]"
          @input="onUsernameInput"
        />
        <!-- Surfaces only when the user just tried to type a
             disallowed character (we silently stripped it). Stays
             invisible otherwise — no permanent rule clutter for
             the 99% of inputs that are fine. -->
        <div v-if="usernameHint" class="auth-hint-line mb-3">{{ usernameHint }}</div>
        <div v-else class="mb-3"></div>

        <label class="auth-label">Display name <span class="auth-optional">(optional)</span></label>
        <input
          v-model="displayName" type="text" autocomplete="name" maxlength="64"
          placeholder="What we call you"
          :class="['input', 'mb-3']"
          @input="onFieldInput"
        />

        <label class="auth-label">Password</label>
        <input
          v-model="password" type="password" autocomplete="new-password"
          placeholder="At least 8 characters"
          :class="['input', 'mb-3', { 'input--err': fieldErr === 'password' }]"
          @input="onFieldInput"
        />

        <label class="auth-label">Confirm password</label>
        <input
          v-model="passwordConfirm" type="password" autocomplete="new-password"
          :class="['input', 'mb-4', { 'input--err': fieldErr === 'confirm' }]"
          @input="onFieldInput"
        />

        <button
          type="submit"
          :disabled="loading"
          class="auth-submit"
        >{{ loading ? 'Creating account…' : 'Create account' }}</button>

        <div v-if="error" class="auth-error">{{ error }}</div>

        <div class="auth-foot">
          Already have an account?
          <router-link to="/login" class="auth-link">Sign in</router-link>
        </div>
      </template>
    </form>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { register as apiRegister, getMe } from '@/api/auth'

const router = useRouter()
const route = useRoute()

const email = ref('')
const username = ref('')
const displayName = ref('')
const password = ref('')
const passwordConfirm = ref('')

// Mirrors the server's _USERNAME_RE — refused at submit time too,
// so we never round-trip a name we know the backend will reject.
const USERNAME_RE = /^[a-zA-Z0-9_-]{3,32}$/
// Pre-fill from ?invitation= in case the user landed via an
// invitation link. Hidden from the form for now (Phase 1 keeps
// invitations admin-driven only via SQL); reserved for later.
const invitationToken = ref(route.query.invitation || null)

const loading = ref(false)
const error = ref('')
const fieldErr = ref('')
// Inline hint shown ONLY when the user types something we had to
// strip (disallowed character). Keeps the form quiet by default
// and educates lazily when the input drifts off the regex.
const usernameHint = ref('')
const successMessage = ref('')
const emailInput = ref(null)
const probing = ref(true)

onMounted(async () => {
  // Already-logged-in users skip straight through. Auth-disabled
  // mode also short-circuits — registration is meaningless then,
  // so we just bounce to chat.
  try {
    const me = await getMe()
    if (me) {
      window.location.href = '/chat'
      return
    }
  } catch {
    // 401 → fall through.
  }
  probing.value = false
  emailInput.value?.focus()
})

function onFieldInput() {
  if (error.value) error.value = ''
  if (fieldErr.value) fieldErr.value = ''
}

// Username gets a tiny extra typing-time guardrail: silently strip
// disallowed characters as the user types so they don't compose a
// long invalid string and only learn about it at submit. When we
// DO strip something, surface a one-liner reminder under the
// field — that's the only moment the rule actually matters to
// the user. Hint clears on the next clean keystroke.
function onUsernameInput() {
  const cleaned = username.value.replace(/[^a-zA-Z0-9_-]/g, '')
  if (cleaned !== username.value) {
    username.value = cleaned
    usernameHint.value = 'Only letters, digits, underscores or hyphens.'
  } else {
    usernameHint.value = ''
  }
  onFieldInput()
}

async function onSubmit() {
  error.value = ''
  fieldErr.value = ''

  const emailVal = email.value.trim()
  const usernameVal = username.value.trim()
  if (!emailVal) {
    error.value = 'Enter your email address.'
    fieldErr.value = 'email'
    return
  }
  if (!emailVal.includes('@') || !emailVal.includes('.')) {
    error.value = "That doesn't look like a valid email."
    fieldErr.value = 'email'
    return
  }
  if (!usernameVal) {
    error.value = 'Choose a username.'
    fieldErr.value = 'username'
    return
  }
  if (!USERNAME_RE.test(usernameVal)) {
    error.value = 'Username must be 3–32 characters using letters, digits, underscores or hyphens.'
    fieldErr.value = 'username'
    return
  }
  if (!password.value) {
    error.value = 'Choose a password.'
    fieldErr.value = 'password'
    return
  }
  if (password.value.length < 8) {
    error.value = 'Password must be at least 8 characters.'
    fieldErr.value = 'password'
    return
  }
  if (password.value !== passwordConfirm.value) {
    error.value = "Passwords don't match."
    fieldErr.value = 'confirm'
    return
  }

  loading.value = true
  try {
    const r = await apiRegister({
      email: emailVal,
      username: usernameVal,
      password: password.value,
      displayName: displayName.value.trim() || null,
      invitationToken: invitationToken.value,
    })
    // Success copy depends on the resulting account status:
    //   - active: ready to sign in (open mode, or first-user admin promotion)
    //   - pending_approval: admin must greenlight before login works
    if (r?.status === 'pending_approval') {
      successMessage.value =
        "Account created — an administrator needs to approve it before you can sign in. " +
        "We'll redirect you to the sign-in page in a few seconds."
    } else {
      successMessage.value =
        "Account created. Redirecting you to sign in…"
    }
    setTimeout(() => router.push('/login'), 2200)
  } catch (e) {
    const status = e?.status
    const detail = (e?.message || '').toLowerCase()
    if (status === 409 && detail.includes('email')) {
      error.value = 'An account with that email already exists.'
      fieldErr.value = 'email'
    } else if (status === 409 && detail.includes('username')) {
      error.value = 'That username is already taken — pick a different one.'
      fieldErr.value = 'username'
    } else if (status === 409) {
      error.value = 'That account already exists.'
    } else if (status === 400 && detail.includes('email')) {
      error.value = "That doesn't look like a valid email address."
      fieldErr.value = 'email'
    } else if (status === 400 && detail.includes('username')) {
      error.value = 'Username must be 3–32 characters using letters, digits, underscores or hyphens.'
      fieldErr.value = 'username'
    } else if (status === 400 && detail.includes('password')) {
      error.value = 'Password is too weak — try something longer.'
      fieldErr.value = 'password'
    } else if (status === 400 && detail.includes('invitation')) {
      error.value = 'Your invitation link is invalid or has expired.'
    } else if (status === 400) {
      error.value = "Something in the form looks off — please double-check and try again."
    } else if (status === 403) {
      error.value =
        "Self-registration isn't open here. Ask an administrator for an invitation."
    } else if (status === 429) {
      error.value = 'Too many attempts. Please wait a moment and try again.'
    } else if (status >= 500) {
      error.value = 'The server hit an error. Try again in a moment.'
    } else if (!status) {
      error.value = "Couldn't reach the server. Check your connection."
    } else {
      error.value = "Couldn't create your account. Please try again."
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* Shares visual language with Login.vue. Kept inline rather than
   factored into a shared partial because the two pages are simple
   enough that an extra component layer hurts readability more than
   the duplication does. */

.auth-shell {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  background: var(--color-bg2);
}
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
  font-size: 11px;
  color: var(--color-t3);
}
.auth-label {
  display: block;
  font-size: 11px;
  color: var(--color-t3);
  margin-bottom: 4px;
}
.auth-optional {
  color: var(--color-t3);
  opacity: 0.7;
}
/* Inline hint shown below an input — only surfaces on demand
   (e.g. when we strip a disallowed character). Keeps the form
   clean by default. */
.auth-hint-line {
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.4;
  color: var(--color-t3);
}
.input--err {
  border-color: var(--color-err-fg, #d23) !important;
  box-shadow: 0 0 0 1px var(--color-err-fg, #d23) inset;
}
.auth-submit {
  width: 100%;
  padding: 9px 12px;
  border-radius: 8px;
  border: 1px solid transparent;
  background: var(--color-t1);
  color: var(--color-bg);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.12s ease, transform 0.06s ease;
}
.auth-submit:hover:not(:disabled) { opacity: 0.92; }
.auth-submit:active:not(:disabled) { transform: translateY(1px); }
.auth-submit:disabled { opacity: 0.55; cursor: not-allowed; }
.auth-error {
  margin-top: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.45;
  color: var(--color-err-fg, #d23);
  background: var(--color-err-bg, rgba(214, 60, 50, 0.08));
  border: 1px solid var(--color-err-line, rgba(214, 60, 50, 0.25));
}
.auth-success {
  padding: 14px 14px 12px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-t1);
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
}
.auth-foot {
  margin-top: 18px;
  text-align: center;
  font-size: 11px;
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
