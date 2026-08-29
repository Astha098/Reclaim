/**
 * Server-render check: run every dashboard component against real API payloads.
 *
 *     node scripts/ssr_check.mjs
 *
 * The npm registry was unreachable while this was built, so there is no Vite and
 * no browser here. What is reachable is React's own server renderer, and that is
 * enough to answer the question that actually matters: does each component execute
 * without throwing when handed the exact payloads `/api/*` returns?
 *
 * That is the failure mode a reviewer would hit first. A component that reads
 * `stats.recovered_value_paise` when the field is called something else, a `.map`
 * over a value that is an object, a division by an absent denominator — all of it
 * surfaces here, on real data from `data/fixture.json`.
 *
 * What this does not cover, stated plainly: `useEffect` does not run during a
 * server render, so this exercises each presentational component with props
 * supplied directly and `App` only in its pre-fetch state. CSS layout is not
 * verified at all. `npm run dev` in `dashboard/` remains the real check, and it is
 * one command.
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, extname, join, relative, resolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const SRC = join(ROOT, 'dashboard', 'src')
const NM = process.env.BABEL_MODULES ?? join(ROOT, 'dashboard', 'node_modules')

async function loadBabel() {
  const at = (...p) => pathToFileURL(join(NM, ...p)).href
  const tries = [
    async () => ({
      core: await import('@babel/core'),
      presetReact: (await import('@babel/preset-react')).default,
      amd: (await import('@babel/plugin-transform-modules-amd')).default,
    }),
    async () => ({
      core: await import(at('@babel', 'core', 'lib', 'index.js')),
      presetReact: (await import(at('@babel', 'preset-react', 'lib', 'index.js'))).default,
      amd: (await import(at('@babel', 'plugin-transform-modules-amd', 'lib', 'index.js')))
        .default,
    }),
  ]
  for (const t of tries) {
    try {
      return await t()
    } catch {
      /* next */
    }
  }
  return null
}

const pkg = await loadBabel()
if (!pkg) {
  console.log('skip: babel not available (set BABEL_MODULES=/path/to/node_modules)')
  process.exit(0)
}
const babel = pkg.core.default ?? pkg.core
const unwrap = (m) => m.default ?? m

const CJS = {
  react: join(NM, 'react', 'cjs', 'react.development.js'),
  'react/jsx-runtime': join(NM, 'react', 'cjs', 'react-jsx-runtime.development.js'),
  'react-dom': join(NM, 'react-dom', 'cjs', 'react-dom.development.js'),
  'react-dom/server': join(NM, 'react-dom', 'cjs', 'react-dom-server-legacy.browser.development.js'),
  scheduler: join(NM, 'scheduler', 'cjs', 'scheduler.development.js'),
}
for (const [id, f] of Object.entries(CJS)) {
  if (!existsSync(f)) {
    console.log(`skip: ${id} not found at ${f}`)
    process.exit(0)
  }
}

const FIXTURE_PATH = join(ROOT, 'data', 'fixture.json')
if (!existsSync(FIXTURE_PATH)) {
  console.log('skip: data/fixture.json missing — run `python3 scripts/fixture.py` first')
  process.exit(0)
}
const fixture = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8'))

// ── module loader ──────────────────────────────────────────────────────────
// The same shape as the browser harness in `render_check.mjs`: CJS for React's
// own builds, AMD for the Babel-transpiled dashboard sources.

const defs = new Map()
const cache = new Map()

function norm(id) {
  const parts = []
  for (const seg of id.split('/')) {
    if (seg === '.' || seg === '') continue
    if (seg === '..') parts.pop()
    else parts.push(seg)
  }
  return './' + parts.join('/')
}

const resolveId = (dep, from) =>
  dep.startsWith('.') ? norm(from.slice(0, from.lastIndexOf('/')) + '/' + dep) : dep

function req(dep, from = './') {
  const id = resolveId(dep, from)
  if (id.endsWith('.css')) return {}
  if (cache.has(id)) return cache.get(id).exports
  const def = defs.get(id)
  if (!def) throw new Error(`module not found: ${id} (from ${from})`)
  const mod = { exports: {} }
  cache.set(id, mod)
  try {
    if (def.kind === 'cjs') {
      const fn = new Function('require', 'module', 'exports', 'process', def.src)
      fn((d) => req(d, id), mod, mod.exports, { env: { NODE_ENV: 'development' } })
    } else {
      let captured = null
      // AMD's `define` is variadic: define(factory), define(deps, factory),
      // define(id, factory), define(id, deps, factory). Babel emits the last of
      // those when `moduleIds` is on, so a fixed two-parameter signature silently
      // binds the module id to `deps`.
      const define = (...a) => {
        const factory = a.pop()
        const deps = Array.isArray(a[a.length - 1]) ? a.pop() : []
        captured = { deps, factory }
      }
      new Function('define', def.src)(define)
      if (!captured) throw new Error(`${id} did not call define()`)
      const args = captured.deps.map((d) =>
        d === 'exports'
          ? mod.exports
          : d === 'module'
            ? mod
            : d === 'require'
              ? (x) => req(x, id)
              : req(d, id),
      )
      captured.factory(...args)
    }
  } catch (e) {
    // Leave no half-initialised module behind. Without this, the first failure
    // poisons the cache and every later case reports the downstream symptom
    // ("element type is invalid") instead of the actual error.
    cache.delete(id)
    throw e
  }
  return mod.exports
}

for (const [id, file] of Object.entries(CJS)) {
  defs.set(id, { kind: 'cjs', src: readFileSync(file, 'utf8') })
}

function walk(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n)
    return statSync(p).isDirectory() ? walk(p) : [p]
  })
}

for (const file of walk(SRC).filter((f) => ['.js', '.jsx'].includes(extname(f)))) {
  const id = './' + relative(SRC, file).split('\\').join('/')
  // Same single patch as the browser harness: `import.meta` has no meaning here.
  const code = readFileSync(file, 'utf8').replace(/import\.meta\.env\.\w+/g, "''")
  const out = babel.transformSync(code, {
    filename: file,
    babelrc: false,
    configFile: false,
    presets: [[unwrap(pkg.presetReact), { runtime: 'automatic', development: false }]],
    plugins: [[unwrap(pkg.amd), {}]],
    moduleIds: true,
    moduleId: id,
    sourceType: 'module',
  })
  defs.set(id, { kind: 'amd', src: out.code })
}

// ── render ─────────────────────────────────────────────────────────────────

const React = req('react')
const server = req('react-dom/server')
const el = React.createElement

// Nothing should reach the network during a server render; if something does, it
// must fail loudly rather than hang the check.
globalThis.fetch = () => Promise.reject(new Error('fetch during SSR'))

const F = (p) => fixture[p]
const stats = F('/api/stats')

const CASES = [
  [
    'Banner (mock)',
    './components/Banner.jsx',
    { config: F('/api/config') },
    ['mock mode', 'simulated'],
  ],
  [
    'Banner (live keys)',
    './components/Banner.jsx',
    { config: { ...F('/api/config'), razorpay_mode: 'test_keys' } },
    ['live keys'],
  ],
  [
    'Headline',
    './components/Headline.jsx',
    { stats, sched: F('/api/scheduler') },
    ['revenue recovered', 'still lost', '₹'],
  ],
  ['Timeline', './components/Timeline.jsx', { timeline: F('/api/timeline') }, ['<svg', 'failed']],
  ['Timeline (empty)', './components/Timeline.jsx', { timeline: [] }, ['No traffic yet']],
  [
    'BucketTable',
    './components/BucketTable.jsx',
    { buckets: F('/api/buckets') },
    ['insufficient funds', 'pending', 'total'],
  ],
  [
    'IssuerPanel',
    './components/IssuerPanel.jsx',
    { issuers: F('/api/issuers'), circuitCfg: F('/api/config').circuit },
    ['card:HDFC', 'open'],
  ],
  [
    'Suppressions',
    './components/Suppressions.jsx',
    { suppressions: F('/api/suppressions'), stats },
    ['not attempted'],
  ],
  [
    'ClassifierPanel',
    './components/ClassifierPanel.jsx',
    { classifier: F('/api/classifier'), config: F('/api/config') },
    ['rules', 'Classifier'],
  ],
  [
    'ActivityFeed',
    './components/ActivityFeed.jsx',
    { attempts: F('/api/attempts?limit=60') },
    ['Decision log', 'attempt'],
  ],
  ['ActivityFeed (empty)', './components/ActivityFeed.jsx', { attempts: [] }, ['No attempts yet']],
  // `App` renders its pre-fetch state here: `useEffect` does not run during a
  // server render, so `data` is still null. That is the loading branch, and it is
  // worth asserting — it is the first thing a reviewer sees on a cold load.
  [
    'App (pre-fetch)',
    './App.jsx',
    {},
    ['Connecting to the recovery engine', 'Failed payments are not lost payments'],
  ],
]

let failed = 0
let totalHtml = 0

// `--dump` prints the rendered text of each case. The assertions above prove the
// components do not throw; reading the text is how you find the bug where they
// render happily and say something wrong.
const DUMP = process.argv.includes('--dump')
const asText = (html) =>
  html
    .replace(/<style[\s\S]*?<\/style>/g, '')
    .replace(/<svg/g, '\n<svg')
    .replace(/<\/(tr|div|p|h1|h2|li|footer|header|section)>/g, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&#x27;|&apos;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/[ \t]+/g, ' ')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .join('\n')

for (const [label, id, props, expect] of CASES) {
  try {
    const Component = req(id).default
    const html = server.renderToStaticMarkup(el(Component, props))
    totalHtml += html.length
    const missing = expect.filter((s) => !html.includes(s))
    if (missing.length) {
      console.log(`  ✗ ${label} — rendered ${html.length}B but missing: ${missing.join(', ')}`)
      failed++
    } else {
      console.log(`  ✓ ${label} — ${html.length}B`)
    }
    if (DUMP) {
      console.log(`\n┌── ${label} ${'─'.repeat(Math.max(0, 60 - label.length))}`)
      for (const line of asText(html).split('\n')) console.log(`│ ${line}`)
      console.log(`└${'─'.repeat(64)}\n`)
    }
  } catch (e) {
    console.log(`  ✗ ${label} — threw: ${String(e.message).split('\n')[0]}`)
    failed++
  }
}

// A few formatting assertions, because a rupee figure that is wrong by a factor of
// 100 renders perfectly happily and is the single most embarrassing possible bug
// on a revenue dashboard. Paise in, display string out; the boundary cases are
// here because that is where an off-by-one order of magnitude hides.
const fmt = req('./fmt.js')
const FMT = [
  ['rupees(14529400)', fmt.rupees(14529400), '₹1,45,294'],
  ['rupeesShort(3050000000)', fmt.rupeesShort(3050000000), '₹3.05Cr'],
  ['rupeesShort(1000000000)', fmt.rupeesShort(1000000000), '₹1.00Cr'], // exactly 1 crore
  ['rupeesShort(999999900)', fmt.rupeesShort(999999900), '₹100.00L'], // one paisa below it
  ['rupeesShort(145294400)', fmt.rupeesShort(145294400), '₹14.53L'],
  ['rupeesShort(500000)', fmt.rupeesShort(500000), '₹5.0K'],
  ['rupeesShort(0)', fmt.rupeesShort(0), '₹0'],
  ['pct(0.2828)', fmt.pct(0.2828), '28.3%'],
  ['hourLabel("2026-08-22T09")', fmt.hourLabel('2026-08-22T09'), '09:00'],
  ['traceKind("✓ ok")', fmt.traceKind('✓ ok'), 'pass'],
  ['traceKind("✗ no")', fmt.traceKind('✗ no'), 'veto'],
  ['traceKind("→ did")', fmt.traceKind('→ did'), 'effect'],
  ['humanize("insufficient_funds")', fmt.humanize('insufficient_funds'), 'insufficient funds'],
]
for (const [label, got, want] of FMT) {
  if (got === want) console.log(`  ✓ ${label} = ${got}`)
  else {
    console.log(`  ✗ ${label} = ${got}  (expected ${want})`)
    failed++
  }
}

// The expanded decision trace is behind a click, so a server render cannot reach
// it — `useState` stays at its initial value. What can be checked without a
// browser is that the payload the expanded row renders is actually there and in
// the shape `ActivityFeed` reads, which is the half of that feature that can go
// wrong silently.
const feed = F('/api/attempts?limit=60')
const traceChecks = [
  ['feed is non-empty', feed.length > 0, `${feed.length} rows`],
  [
    'every row carries a trace',
    feed.every((r) => Array.isArray(r.decision_trace) && r.decision_trace.length > 0),
    `${Math.min(...feed.map((r) => r.decision_trace.length))}–${Math.max(
      ...feed.map((r) => r.decision_trace.length),
    )} lines per row`,
  ],
  [
    'traces carry the glyphs the feed colourises on',
    ['✓', '✗', '→'].every((g) => feed.some((r) => r.decision_trace.some((l) => l.includes(g)))),
    'pass / veto / effect all present',
  ],
  [
    'log is not one homogeneous slab',
    new Set(feed.map((r) => `${r.bucket}/${r.action}/${r.outcome}`)).size >= 5,
    `${new Set(feed.map((r) => `${r.bucket}/${r.action}/${r.outcome}`)).size} distinct ` +
      `bucket/action/outcome combinations in the newest ${feed.length}`,
  ],
]
for (const [label, ok, detail] of traceChecks) {
  console.log(`  ${ok ? '✓' : '✗'} ${label}  — ${detail}`)
  if (!ok) failed++
}

console.log('─'.repeat(68))
const total = CASES.length + FMT.length + traceChecks.length
if (failed) {
  console.log(`FAILED ${failed}/${total}`)
  process.exit(1)
}
console.log(`PASSED ${total}/${total} · ${Math.round(totalHtml / 1024)} KB of markup rendered`)
