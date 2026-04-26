import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', component: () => import('@/views/Login.vue'), meta: { public: true } },
    { path: '/chat', component: () => import('@/views/Chat.vue') },
    { path: '/workspace', component: () => import('@/views/Workspace.vue') },
    {
      // Legacy redirect: old /repository?doc=X links land on /workspace?doc=X.
      // The doc detail view lives inline inside Workspace now (Repository.vue
      // is still used as the embedded component; only the standalone route
      // is retired).
      path: '/repository',
      redirect: (to) => ({ path: '/workspace', query: to.query }),
    },
    { path: '/ingestion', redirect: '/workspace' },
    { path: '/knowledge-graph', component: () => import('@/views/KnowledgeGraph.vue') },
    { path: '/simulation', component: () => import('@/views/Simulation.vue') },
    { path: '/metrics', component: () => import('@/views/Metrics.vue') },
    { path: '/benchmark', component: () => import('@/views/Benchmark.vue') },
    { path: '/tokens', component: () => import('@/views/Tokens.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})

export default router
