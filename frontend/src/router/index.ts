import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'
import DrillGapView from '@/views/DrillGapView.vue'
import DrillGapDetailView from '@/views/DrillGapDetailView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/drill_path/gaps', name: 'drill-gaps', component: DrillGapView },
    {
      path: '/drill_path/gaps/:gapId',
      name: 'drill-gap-detail',
      component: DrillGapDetailView,
      props: (route) => ({ gapId: String(route.params.gapId) }),
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
