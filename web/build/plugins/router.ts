import type { RouteMeta } from 'vue-router';
import type { RouteKey } from '@elegant-router/types';
import ElegantVueRouter from '@elegant-router/vue/vite';

const ROUTE_META_MAP: Record<string, Partial<RouteMeta>> = {
  home: { icon: 'mdi:monitor-dashboard', order: 1 },
  inpatient: { icon: 'mdi:hospital-box-outline', order: 2, roles: ['DEPT_HEAD', 'INSURANCE_OFFICER', 'ADMIN'] },
  calculator: { icon: 'mdi:calculator-variant-outline', order: 3 },
  analytics: { icon: 'mdi:chart-line', order: 4 },
  reconciliation: { icon: 'mdi:file-document-check-outline', order: 5, roles: ['INSURANCE_OFFICER', 'ADMIN'] },
  admin: { icon: 'mdi:shield-account-outline', order: 6, roles: ['INSURANCE_OFFICER', 'ADMIN'] },
  'admin_dip-rules': { icon: 'mdi:database-import-outline', order: 1, roles: ['INSURANCE_OFFICER', 'ADMIN'] },
  'admin_audit-logs': { icon: 'mdi:shield-check-outline', order: 2, roles: ['ADMIN'] },
  admin_operators: { icon: 'mdi:account-cog-outline', order: 3, roles: ['ADMIN'] }
};

export function setupElegantRouter() {
  return ElegantVueRouter({
    layouts: {
      base: 'src/layouts/base-layout/index.vue',
      blank: 'src/layouts/blank-layout/index.vue'
    },
    routePathTransformer(routeName, routePath) {
      const key = routeName as RouteKey;

      if (key === 'login') {
        const modules: UnionKey.LoginModule[] = ['pwd-login', 'code-login', 'register', 'reset-pwd', 'bind-wechat'];

        const moduleReg = modules.join('|');

        return `/login/:module(${moduleReg})?`;
      }

      return routePath;
    },
    onRouteMetaGen(routeName) {
      const key = routeName as RouteKey;

      const constantRoutes: RouteKey[] = ['login', '403', '404', '500'];

      const meta: Partial<RouteMeta> = {
        title: key,
        i18nKey: `route.${key}` as App.I18n.I18nKey,
        ...(ROUTE_META_MAP[key] || {})
      };

      if (constantRoutes.includes(key)) {
        meta.constant = true;
      }

      return meta;
    }
  });
}
