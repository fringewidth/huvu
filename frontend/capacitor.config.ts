import type { CapacitorConfig } from '@capacitor/cli';
import { PAPER } from './src/config/palette.ts';

// Android's WebView reads only hex here, and the palette is in rgb().
function hex(rgb: string): string {
  const channels = rgb.match(/\d+/g) ?? [];
  return '#' + channels.map((c) => Number(c).toString(16).padStart(2, '0')).join('');
}

const config: CapacitorConfig = {
  appId: 'com.huvu.app',
  appName: 'huvu',
  webDir: 'dist',
  // The garden's paper color, so the WebView never flashes white while
  // the page loads.
  backgroundColor: hex(PAPER),
};

export default config;
