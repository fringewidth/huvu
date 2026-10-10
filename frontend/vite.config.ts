import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
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

// Stamps the build with bundle.json: a hash of every file in it, and the
// native version it needs (native.json). A native app reads its own stamp
// and the host's, and live-updates when the hashes differ
// (src/net/liveUpdate.ts, backend/src/huvu/bundles.py). The hash is of
// the files alone, so the APK and the host built from the same sources
// agree, and the phone downloads nothing.
function stamp(): Plugin {
  const name = 'bundle.json'
  let outDir = ''
  return {
    name: 'huvu-bundle-stamp',
    apply: 'build',
    configResolved(config) {
      outDir = resolve(config.root, config.build.outDir)
    },
    closeBundle() {
      const native = JSON.parse(readFileSync(new URL('./native.json', import.meta.url), 'utf8')).version
      const files = readdirSync(outDir, { recursive: true, withFileTypes: true })
        .filter((entry) => entry.isFile())
        .map((entry) => relative(outDir, join(entry.parentPath, entry.name)).split(sep).join('/'))
        .filter((file) => file !== name)
        .sort()
      const hash = createHash('sha256')
      for (const file of files) {
        hash.update(file).update('\0').update(readFileSync(join(outDir, file))).update('\0')
      }
      writeFileSync(join(outDir, name), JSON.stringify({ version: hash.digest('hex').slice(0, 16), native }))
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), colors(), stamp()],
})
