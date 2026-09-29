import { readFileSync } from 'node:fs'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'
import * as palette from './src/config/palette.ts'

// Fills each %NAME% placeholder with that color from config/palette.ts.
// Throws on a name the palette doesn't have, so a typo can't ship a
// broken color.
function fillColors(text: string): string {
  return text.replace(/%([A-Z_]+)%/g, (_, name: string) => {
    const color = (palette as Record<string, string>)[name]
    if (!color) throw new Error(`palette has no color named ${name}`)
    return color
  })
}

// Colors index.html, which also defines the CSS color tokens, so the page
// is paper-colored before any CSS loads. The favicon is colored from
// src/favicon.svg and inlined as a data URL.
function colors(): Plugin {
  return {
    name: 'huvu-colors',
    transformIndexHtml(html) {
      const favicon = fillColors(readFileSync(new URL('./src/favicon.svg', import.meta.url), 'utf8'))
      const href = `data:image/svg+xml,${encodeURIComponent(favicon)}`
      // Split around %FAVICON% first, so the percent escapes in the
      // data URL are never read as placeholders.
      return html.split('%FAVICON%').map(fillColors).join(href)
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), colors()],
})
