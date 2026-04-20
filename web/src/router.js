import { createRouter, createWebHistory } from 'vue-router'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/chat', component: () => import('@/views/Chat.vue') },
    { path: '/architecture', component: () => import('@/views/Architecture.vue') },
    { path: '/workspace', component: () => import('@/views/Workspace.vue') },
    { path: '/repository', component: () => import('@/views/Repository.vue') },
    { path: '/ingestion', redirect: '/repository' },
    { path: '/knowledge-graph', component: () => import('@/views/KnowledgeGraph.vue') },
    { path: '/benchmark', component: () => import('@/views/Benchmark.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})
