import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.huvu.app',
  appName: 'huvu',
  webDir: 'dist',
  // The garden's paper color, so the WebView never flashes white while
  // the page loads.
  backgroundColor: '#0d0e17',
};

export default config;
