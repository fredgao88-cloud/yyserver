import { addCollection } from '@iconify/vue';
import type { IconifyJSON } from '@iconify/vue';
import offlineIcons from './offline-icons.json';

/** Setup offline icon collections so zero external api.iconify.design calls occur */
export function setupIconifyOffline() {
  for (const col of offlineIcons as IconifyJSON[]) {
    addCollection(col);
  }
}
