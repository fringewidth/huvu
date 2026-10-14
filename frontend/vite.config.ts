import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv, type Plugin } from 'vite'
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

// The web app manifest and the home screen icon, so huvu can be added to a
// phone's home screen, where it opens in its own window and gets
// notifications (src/account/webPush.ts). The manifest is colored from
// src/manifest.json, served by the dev server and written into the build.
// The links are added after Vite reads the page, so it doesn't look for the
// manifest among the sources.
function manifest(): Plugin {
  const name = 'manifest.webmanifest'
  const source = () => fillColors(readFileSync(new URL('./src/manifest.json', import.meta.url), 'utf8'))
  let base = '/'
  return {
    name: 'huvu-manifest',
    configResolved(config) {
      base = config.base
    },
    configureServer(server) {
      server.middlewares.use(`${base}${name}`, (_req, res) => {
        res.setHeader('Content-Type', 'application/manifest+json')
        res.end(source())
      })
    },
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: name, source: source() })
    },
    transformIndexHtml: {
      order: 'post',
      handler: () => [
        { tag: 'link', attrs: { rel: 'manifest', href: `${base}${name}` }, injectTo: 'head' },
        { tag: 'link', attrs: { rel: 'apple-touch-icon', href: `${base}icons/apple-touch-icon.png` }, injectTo: 'head' },
      ],
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

// The host's address, for the phone's build alone: Capacitor serves that
// build from the APK, so it can't use its page's own origin as the web and
// TV builds do. It is HUVU_PUBLIC_URL, from the environment or the repo's
// .env (gitignored), the setting compose.yaml reads too, so no address is
// written in the code.
function publicUrl(mode: string, command: string): string | undefined {
  if (mode !== 'phone' || command !== 'build') return undefined
  const url = process.env.HUVU_PUBLIC_URL || loadEnv(mode, resolve(import.meta.dirname, '..'), 'HUVU_').HUVU_PUBLIC_URL
  if (!url) throw new Error("the phone's build needs the host's address: set HUVU_PUBLIC_URL in the repo's .env")
  return url.replace(/\/$/, '')
}

// https://vite.dev/config/
export default defineConfig(({ mode, command }) => {
  const url = publicUrl(mode, command)
  return {
    plugins: [react(), tailwindcss(), colors(), manifest(), stamp()],
    define: url ? { 'import.meta.env.VITE_PUBLIC_URL': JSON.stringify(url) } : {},
  }
})
