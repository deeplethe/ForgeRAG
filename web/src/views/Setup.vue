<template>
  <div class="setup-shell">
    <!-- Hide the form until status comes back so we don't flash the
         wizard at users who already configured the deploy. -->
    <div v-if="loading" class="setup-card setup-card--centered">
      <Spinner :size="22" />
      <span class="loading-label">Loading…</span>
    </div>

    <!-- Already configured → bounce. (Belt-and-suspenders; the
         router gate in App.vue should have redirected already.) -->
    <div v-else-if="alreadyConfigured" class="setup-card setup-card--centered">
      <div class="big-emoji">✅</div>
      <h2 class="setup-title">Setup is complete</h2>
      <p class="setup-subtitle">This deploy is already configured.</p>
      <button class="btn-primary" @click="router.push('/login')">Continue</button>
    </div>

    <!-- ── Step 1: pick a preset ───────────────────────────────────── -->
    <div v-else-if="step === 'pick'" class="setup-card setup-card--wide">
      <div class="setup-brand">
        <img src="/craig.png" alt="" class="setup-logo" />
        <span class="wordmark text-[20px]">OpenCraig</span>
      </div>
      <h2 class="setup-title">Configure your model platform</h2>
      <p class="setup-subtitle">
        Pick a unified provider for chat, embeddings, and reranking. You
        can change all of this later in Settings; this step just gets
        you running.
      </p>

      <div class="preset-grid">
        <button
          v-for="p in presets"
          :key="p.id"
          type="button"
          class="preset-tile"
          :class="{ 'is-selected': selectedPreset?.id === p.id }"
          @click="onPickPreset(p)"
        >
          <span class="preset-emoji">{{ p.logo_emoji }}</span>
          <div class="preset-meta">
            <span class="preset-name">{{ p.name }}</span>
            <span class="preset-tag">{{ localeIsZh ? p.tagline : p.tagline_en }}</span>
          </div>
          <div v-if="p.recommended_for.includes('china')" class="preset-badge">CN</div>
          <div v-else-if="p.recommended_for.includes('high_compliance')" class="preset-badge">Air-gap</div>
        </button>
      </div>

      <div class="setup-foot">
        Already using OpenCraig?
        <a href="/login" class="setup-link">Sign in</a>
      </div>
    </div>

    <!-- ── Step 2: enter inputs for the chosen preset ──────────────── -->
    <div v-else-if="step === 'inputs' && selectedPreset" class="setup-card">
      <button class="back-btn" @click="step = 'pick'; testResult = null">
        <ChevronLeft :size="14" :stroke-width="1.75" />
        <span>Back</span>
      </button>

      <div class="picked-summary">
        <span class="preset-emoji">{{ selectedPreset.logo_emoji }}</span>
        <div>
          <div class="preset-name">{{ selectedPreset.name }}</div>
          <div class="preset-tag">{{ localeIsZh ? selectedPreset.tagline : selectedPreset.tagline_en }}</div>
        </div>
      </div>

      <!-- Custom preset has no inputs → drop straight to commit -->
      <div v-if="!selectedPreset.inputs.length" class="custom-hint">
        Custom mode skips the wizard. After commit, configure each
        provider in Settings → System (admin only).
      </div>

      <template v-else>
        <div v-for="input in selectedPreset.inputs" :key="input.name" class="input-row">
          <label class="auth-label">
            {{ input.label }}
            <a v-if="input.help && input.help.startsWith('http')" :href="input.help"
               target="_blank" rel="noopener" class="help-link">↗</a>
            <span v-else-if="input.help" class="help-line">{{ input.help }}</span>
          </label>
          <input
            v-model="inputs[input.name]"
            :type="input.secret ? 'password' : 'text'"
            :placeholder="input.placeholder || ''"
            class="input"
            @input="testResult = null"
          />
        </div>
      </template>

      <!-- Test result (success or error) -->
      <div v-if="testResult" class="test-result"
           :class="{ 'is-ok': testResult.ok, 'is-err': !testResult.ok }">
        <span v-if="testResult.ok">
          ✓ Connected to <code>{{ testResult.model || selectedPreset.name }}</code>
          <span v-if="testResult.latency_ms">&nbsp;({{ testResult.latency_ms }} ms)</span>
        </span>
        <span v-else>✗ {{ testResult.error }}</span>
      </div>

      <div v-if="errorMsg" class="setup-error">{{ errorMsg }}</div>

      <div class="setup-actions">
        <button
          v-if="selectedPreset.inputs.length && !selectedPreset.skip_test"
          type="button"
          class="btn-secondary"
          :disabled="testing || !canSubmit"
          @click="onTest"
        >{{ testing ? 'Testing…' : 'Test connection' }}</button>

        <button
          type="button"
          class="btn-primary"
          :disabled="committing || !canSubmit"
          @click="onCommit"
        >{{ committing ? 'Saving…' : 'Apply & continue' }}</button>
      </div>
    </div>

    <!-- ── Step 3: restarting / done ──────────────────────────────── -->
    <div v-else-if="step === 'restart'" class="setup-card setup-card--centered">
      <Spinner :size="22" />
      <h2 class="setup-title" style="margin-top: 16px">Applying configuration</h2>
      <p class="setup-subtitle">
        OpenCraig is restarting to load your new settings — this usually
        takes 5–15 seconds. We'll redirect you to sign-in automatically.
      </p>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ChevronLeft } from 'lucide-vue-next'
import {
  getSetupStatus,
  listSetupPresets,
  testSetupLlm,
  commitSetup,
} from '@/api/setup'
import Spinner from '@/components/Spinner.vue'

const router = useRouter()
const { locale } = useI18n()
const localeIsZh = computed(() => (locale.value || '').startsWith('zh'))

// ── State ──────────────────────────────────────────────────────
const loading = ref(true)
const alreadyConfigured = ref(false)
const presets = ref([])
const step = ref('pick')          // 'pick' | 'inputs' | 'restart'
const selectedPreset = ref(null)
const inputs = reactive({})

const testing = ref(false)
const testResult = ref(null)
const committing = ref(false)
const errorMsg = ref('')

let _restartPoll = null

const canSubmit = computed(() => {
  if (!selectedPreset.value) return false
  if (!selectedPreset.value.inputs.length) return true
  return selectedPreset.value.inputs.every((i) => {
    const v = inputs[i.name]
    return typeof v === 'string' && v.trim().length > 0
  })
})

// ── Lifecycle ──────────────────────────────────────────────────
onMounted(async () => {
  try {
    const [status, presetList] = await Promise.all([
      getSetupStatus(),
      listSetupPresets(),
    ])
    if (status?.configured) {
      alreadyConfigured.value = true
      // Best-effort redirect after a beat so users see why we're
      // bouncing (and can hit Continue manually if it stalls).
      setTimeout(() => router.push('/login'), 800)
    } else {
      presets.value = presetList || []
    }
  } catch (e) {
    errorMsg.value = "Couldn't reach the setup endpoint — is the server running?"
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(() => {
  if (_restartPoll) clearInterval(_restartPoll)
})

// ── Handlers ───────────────────────────────────────────────────
function onPickPreset(p) {
  selectedPreset.value = p
  // Pre-fill input defaults if the preset declared any (Ollama URL
  // does this).
  for (const k of Object.keys(inputs)) delete inputs[k]
  for (const inp of p.inputs || []) {
    inputs[inp.name] = inp.default || ''
  }
  testResult.value = null
  errorMsg.value = ''
  step.value = 'inputs'
}

async function onTest() {
  if (!selectedPreset.value) return
  testing.value = true
  errorMsg.value = ''
  testResult.value = null
  try {
    const r = await testSetupLlm(selectedPreset.value.id, { ...inputs })
    testResult.value = r
  } catch (e) {
    testResult.value = { ok: false, error: friendlyError(e) }
  } finally {
    testing.value = false
  }
}

async function onCommit() {
  if (!selectedPreset.value) return
  committing.value = true
  errorMsg.value = ''
  try {
    const r = await commitSetup(selectedPreset.value.id, { ...inputs })
    if (!r?.ok) {
      errorMsg.value = "The server didn't accept that configuration."
      return
    }
    // Switch to "restarting" view; poll status until the backend
    // comes back configured=True, then redirect.
    step.value = 'restart'
    _startRestartPoll()
  } catch (e) {
    errorMsg.value = friendlyError(e)
  } finally {
    committing.value = false
  }
}

function _startRestartPoll() {
  let attempts = 0
  const MAX = 60        // ~60 attempts × 1.5s = 90s tolerance
  _restartPoll = setInterval(async () => {
    attempts++
    if (attempts > MAX) {
      clearInterval(_restartPoll)
      errorMsg.value =
        'The server has not come back up. Check ``docker compose logs opencraig`` ' +
        'or restart the container manually.'
      step.value = 'inputs'
      return
    }
    try {
      const r = await getSetupStatus()
      if (r?.configured) {
        clearInterval(_restartPoll)
        // Hard reload so cached pages re-fetch /me, /folders/spaces, etc.
        window.location.href = '/login'
      }
    } catch {
      // Backend mid-restart — endpoint unreachable. Keep polling.
    }
  }, 1500)
}

function friendlyError(e) {
  const status = e?.status
  const detail = (e?.message || '').toLowerCase()
  if (status === 403 && detail.includes('already')) {
    return 'Setup is already complete on this server.'
  }
  if (!status) return "Couldn't reach the server."
  if (status >= 500) return 'The server hit an error. Try again in a moment.'
  return e?.message || 'Something went wrong.'
}
</script>

<style scoped>
.setup-shell {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  background: var(--color-bg2);
}

.setup-card {
  width: 100%;
  max-width: 480px;
  padding: 28px;
  border: 1px solid var(--color-line);
  border-radius: 12px;
  background: var(--color-bg);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
  position: relative;
}
.setup-card--centered {
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 36px 28px;
}
.setup-card--wide {
  max-width: 560px;
}

.setup-brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  margin-bottom: 18px;
}
.setup-logo {
  width: 44px;
  height: 44px;
  border-radius: 999px;
}

.setup-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--color-t1);
  margin: 0 0 6px;
  text-align: center;
}
.setup-subtitle {
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-t3);
  margin: 0 0 20px;
  text-align: center;
}

.big-emoji { font-size: 36px; }

/* ── Preset grid ───────────────────────────────────────────── */
.preset-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 18px;
}
.preset-tile {
  display: flex;
  align-items: center;
  gap: 14px;
  width: 100%;
  padding: 14px 16px;
  background: var(--color-bg);
  border: 1px solid var(--color-line);
  border-radius: 10px;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.12s ease, background 0.12s ease;
}
.preset-tile:hover {
  border-color: var(--color-line2);
  background: var(--color-bg2);
}
.preset-tile.is-selected {
  border-color: var(--color-t1);
  background: var(--color-bg2);
}
.preset-emoji {
  font-size: 22px;
  line-height: 1;
  flex-shrink: 0;
}
.preset-meta { flex: 1; min-width: 0; }
.preset-name {
  display: block;
  font-size: 13px;
  font-weight: 500;
  color: var(--color-t1);
}
.preset-tag {
  display: block;
  font-size: 11px;
  color: var(--color-t3);
  line-height: 1.4;
  margin-top: 2px;
}
.preset-badge {
  font-size: 10px;
  font-weight: 500;
  padding: 2px 6px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--color-accent, #6366f1) 12%, transparent);
  color: var(--color-accent, #6366f1);
  flex-shrink: 0;
}

/* ── Step 2: inputs ─────────────────────────────────────────── */
.back-btn {
  position: absolute;
  top: 16px;
  left: 14px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  background: transparent;
  border: none;
  border-radius: 6px;
  color: var(--color-t3);
  font-size: 12px;
  cursor: pointer;
}
.back-btn:hover { color: var(--color-t1); background: var(--color-bg2); }

.picked-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 14px;
  margin: 18px 0 22px;
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: 8px;
}

.input-row { margin-bottom: 14px; }
.auth-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--color-t3);
  margin-bottom: 4px;
}
.help-link {
  color: var(--color-t1);
  text-decoration: none;
  font-size: 11px;
}
.help-link:hover { text-decoration: underline; }
.help-line {
  font-weight: 400;
  opacity: 0.85;
}

.custom-hint {
  padding: 12px 14px;
  background: var(--color-bg2);
  border: 1px solid var(--color-line);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-t2);
  margin-bottom: 14px;
}

.test-result {
  margin: 12px 0;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.45;
  border: 1px solid var(--color-line);
}
.test-result.is-ok {
  color: #0f766e;
  background: color-mix(in srgb, #14b8a6 12%, transparent);
  border-color: color-mix(in srgb, #14b8a6 35%, transparent);
}
.test-result.is-ok code {
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: 11px;
  padding: 1px 5px;
  background: var(--color-bg);
  border-radius: 3px;
}
.test-result.is-err {
  color: var(--color-err-fg, #b91c1c);
  background: var(--color-err-bg, rgba(214, 60, 50, 0.08));
  border-color: var(--color-err-line, rgba(214, 60, 50, 0.25));
}

.setup-error {
  margin: 12px 0 0;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 11px;
  color: var(--color-err-fg, #b91c1c);
  background: var(--color-err-bg, rgba(214, 60, 50, 0.08));
  border: 1px solid var(--color-err-line, rgba(214, 60, 50, 0.25));
}

.setup-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  margin-top: 18px;
}

.btn-primary, .btn-secondary {
  height: 32px;
  padding: 0 14px;
  border-radius: 8px;
  border: 1px solid transparent;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.12s ease, background 0.12s ease;
}
.btn-primary {
  background: var(--color-t1);
  color: var(--color-bg);
}
.btn-primary:hover:not(:disabled) { opacity: 0.92; }
.btn-primary:disabled { opacity: 0.45; cursor: not-allowed; }
.btn-secondary {
  background: var(--color-bg);
  border-color: var(--color-line);
  color: var(--color-t1);
}
.btn-secondary:hover:not(:disabled) { background: var(--color-bg2); }
.btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }

.setup-foot {
  margin-top: 18px;
  text-align: center;
  font-size: 11px;
  color: var(--color-t3);
}
.setup-link {
  color: var(--color-t1);
  text-decoration: none;
  border-bottom: 1px solid var(--color-line);
  padding-bottom: 1px;
}
.setup-link:hover { border-bottom-color: var(--color-t1); }

.loading-label {
  font-size: 12px;
  color: var(--color-t3);
  margin-top: 6px;
}
</style>
