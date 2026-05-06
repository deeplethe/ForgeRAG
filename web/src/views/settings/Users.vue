<script setup>
/**
 * /settings/users — admin-only user list.
 *
 * V1 stub: just renders a placeholder. Real implementation
 * (step 3 of the auth refactor) will fetch from a not-yet-built
 * /api/v1/admin/users endpoint and render a table with role
 * change + suspend/reinstate actions inline. Hooks into the
 * existing AuthUser status lifecycle (active / pending_approval
 * / suspended / deleted).
 */
import { useI18n } from 'vue-i18n'
const { t } = useI18n()
</script>

<template>
  <div class="users-page">
    <h2 class="page-title">{{ t('settings.users.title') }}</h2>
    <div class="placeholder">
      {{ t('settings.users.placeholder') }}
    </div>
  </div>
</template>

<style scoped>
.users-page { max-width: 800px; }
.page-title {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--color-t1);
  margin: 0 0 20px;
}
.placeholder {
  border: 1px dashed var(--color-line);
  border-radius: var(--r-md);
  padding: 32px;
  text-align: center;
  color: var(--color-t3);
  font-size: 12px;
}
</style>
