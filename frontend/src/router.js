import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue') },
  {
    path: '/',
    component: () => import('./views/Layout.vue'),
    children: [
      { path: '', component: () => import('./views/Dashboard.vue') },
      { path: 'readings', component: () => import('./views/Readings.vue') },
      { path: 'runs', component: () => import('./views/Runs.vue') },
      { path: 'runs/:id', component: () => import('./views/RunDetail.vue') },
      { path: 'runs/:id/regen', component: () => import('./views/Regen.vue') },
      { path: 'metrics', component: () => import('./views/Metrics.vue') },
      { path: 'replay', component: () => import('./views/Replay.vue') },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })
router.beforeEach((to) => {
  if (to.path !== '/login' && !localStorage.getItem('token')) return '/login'
})
export default router
