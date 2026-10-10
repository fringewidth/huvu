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
  plugins: {
    // The app in front shows everything itself, from the push stream, so
    // iOS shows no notification over it. Android does this by default.
    FirebaseMessaging: {
      presentationOptions: [],
    },
    // Live updates (src/net/liveUpdate.ts) come from huvu's own host, and
    // the app decides when. Capgo's cloud gets no update checks and no
    // stats.
    CapacitorUpdater: {
      autoUpdate: false,
      statsUrl: '',
    },
  },
};

export default config;
