<template>
  <div class="simulation-root">
    <!-- Left: parameters form + presets + run button -->
    <aside class="sim-left">
      <ParamsPanel @run="onRun" />
    </aside>

    <!-- Right: page header + timeline card + detail card -->
    <section class="sim-right">
      <header class="sim-header">
        <div class="text-[13px] text-t1 font-medium">Retrieval Simulation</div>
        <div class="text-[11px] text-t3 mt-0.5">
          Dry-run one query, inspect every stage. Answer response is discarded —
          no conversation is created.
        </div>
      </header>

      <div class="sim-body">
        <div class="panel sim-card sim-timeline">
          <PipelineTimeline />
        </div>
        <div class="panel sim-card sim-detail">
          <StageDetail />
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { onMounted } from 'vue'
import { useSimulationStore } from '@/stores/simulation'
import { post } from '@/api/client'
import ParamsPanel from '@/components/simulation/ParamsPanel.vue'
import PipelineTimeline from '@/components/simulation/PipelineTimeline.vue'
import StageDetail from '@/components/simulation/StageDetail.vue'

const store = useSimulationStore()

// Pull resolved cfg defaults so the form starts populated with the actual
// values the backend will use — no "default" placeholder UI.
onMounted(() => { store.ensureDefaults() })

async function onRun() {
  if (!store.params.query?.trim()) {
    store.setError('Enter a query first')
    return
  }
  store.startRun()
  try {
    // The standard /query endpoint returns trace + stats + answer. The answer
    // comes along for the ride but we surface the stages, not the text. No
    // conversation_id → backend does not persist this run.
    const res = await post('/api/v1/query', store.requestBody)
    store.setResult(res)
  } catch (e) {
    store.setError(e?.message || String(e))
  }
}
</script>

<style scoped>
.simulation-root {
  display: flex;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  background: var(--color-bg2);   /* canvas — both sides + body */
}

.sim-left {
  width: 280px;
  flex-shrink: 0;
  border-right: 1px solid var(--color-line);
  overflow-y: auto;
  /* inherits canvas (bg2) from parent .simulation-root */
}

.sim-right {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.sim-header {
  padding: 18px 24px 14px;
  flex-shrink: 0;
}

.sim-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 0 24px 24px;
  overflow-y: auto;
}

.sim-card { padding: 18px 20px; }
.sim-timeline { flex-shrink: 0; min-height: 180px; }
.sim-detail   { flex: 1; min-height: 240px; padding: 0; overflow: hidden; }
</style>
