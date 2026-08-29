/**
 * Render check: build the dashboard into one self-contained HTML file.
 *
 *     node scripts/render_check.mjs            # → $TMPDIR/reclaim-render.html
 *     node scripts/render_check.mjs --out x.html
 *
 * Why this exists. The dashboard was developed in an environment with no access to
 * the npm registry, so `vite` could not be installed and `npm run build` could not
 * be run. Shipping a frontend that has never been rendered is not acceptable for a
 * project whose entire point is the demo, so this reproduces just enough of a
 * bundler to get the real components in front of a real React: Babel transpiles
 * each source file to AMD, React's own CJS builds are inlined, and a ~40-line
 * loader wires them together in the page.
 *
 * `window.fetch` is replaced with a fixture reader (`data/fixture.json`, from
 * `scripts/fixture.py`), so the components render against exactly the payloads the
 * API returns without a backend or a socket.
 *
 * What this does NOT verify: Vite's own transform pipeline, CSS processing, HMR,
 * and production minification. It verifies that the component code renders, that
 * the data plumbing matches the API contract, and that the layout is what was
 * intended. `npm run build` remains the real gate.
 *
 * One source patch is applied, and only one: `import.meta.env.VITE_API_BASE` in
 * `api.js` becomes `''`, because `import.meta` has no meaning outside ESM. Every
 * other byte of every component is the code that ships.
 */

import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { dirname, extname, join, relative, resolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const SRC = join(ROOT, 'dashboard', 'src')

const outArg = process.argv.indexOf('--out')
const OUT = outArg > 0 ? process.argv[outArg + 1] : join(tmpdir(), 'reclaim-render.html')

async function loadBabel() {
  const dirs = [null, process.env.BABEL_MODULES].filter((d) => d !== undefined)
  for (const dir of dirs) {
    try {
      if (dir === null) {
        return {
          core: await import('@babel/core'),
          presetReact: (await import('@babel/preset-react')).default,
          amd: (await import('@babel/plugin-transform-modules-amd')).default,
        }
      }
      const at = (p) => pathToFileURL(join(dir, ...p)).href
      return {
        core: await import(at(['@babel', 'core', 'lib', 'index.js'])),
        presetReact: (await import(at(['@babel', 'preset-react', 'lib', 'index.js']))).default,
        amd: (await import(at(['@babel', 'plugin-transform-modules-amd', 'lib', 'index.js'])))
          .default,
      }
    } catch {
      /* try the next location */
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

// React's own builds. Resolved from the same place Babel came from, since that is
// where an offline install would have put them.
const NM = process.env.BABEL_MODULES ?? join(ROOT, 'dashboard', 'node_modules')
const REACT_MODULES = {
  react: join(NM, 'react', 'cjs', 'react.development.js'),
  'react/jsx-runtime': join(NM, 'react', 'cjs', 'react-jsx-runtime.development.js'),
  'react-dom': join(NM, 'react-dom', 'cjs', 'react-dom.development.js'),
  'react-dom/client': join(NM, 'react-dom', 'cjs', 'react-dom-client.development.js'),
  scheduler: join(NM, 'scheduler', 'cjs', 'scheduler.development.js'),
}

for (const [id, file] of Object.entries(REACT_MODULES)) {
  if (!existsSync(file)) {
    console.log(`skip: cannot find ${id} at ${file}`)
    process.exit(0)
  }
}

function walk(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n)
    return statSync(p).isDirectory() ? walk(p) : [p]
  })
}

const files = walk(SRC).filter((f) => ['.js', '.jsx'].includes(extname(f)))
const modules = []

for (const file of files) {
  const id = './' + relative(SRC, file).split('\\').join('/')
  let code = readFileSync(file, 'utf8')
  // The single patch, announced in the header above.
  code = code.replace(/import\.meta\.env\?\.\w+|import\.meta\.env\.\w+/g, "''")
  const out = babel.transformSync(code, {
    filename: file,
    babelrc: false,
    configFile: false,
    presets: [[unwrap(pkg.presetReact), { runtime: 'automatic', development: true }]],
    plugins: [[unwrap(pkg.amd), { noInterop: false }]],
    moduleIds: true,
    moduleId: id,
    sourceType: 'module',
  })
  modules.push({ id, code: out.code })
}

const esc = (s) => s.replaceAll('</script', '<\\/script')

const cjsBlocks = Object.entries(REACT_MODULES)
  .map(
    ([id, file]) =>
      `__cjs(${JSON.stringify(id)}, function (require, module, exports) {\n${esc(
        readFileSync(file, 'utf8'),
      )}\n});`,
  )
  .join('\n')

const fixturePath = join(ROOT, 'data', 'fixture.json')
if (!existsSync(fixturePath)) {
  console.log('skip: data/fixture.json missing — run `python3 scripts/fixture.py` first')
  process.exit(0)
}
const fixture = readFileSync(fixturePath, 'utf8')
const css = readFileSync(join(SRC, 'styles.css'), 'utf8')

const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reclaim — render check</title>
<style>${css}</style>
</head><body><div id="root"></div>
<script>
// ── minimal module loader ───────────────────────────────────────────────────
// Enough of CJS and AMD to run React and the transpiled components. Not a
// bundler; a harness.
var __defs = {}, __cache = {}, __pendingId = null;

function __norm(id) {
  var parts = [], segs = id.split('/');
  for (var i = 0; i < segs.length; i++) {
    if (segs[i] === '.' || segs[i] === '') { if (i === 0) parts.push(segs[i]); continue; }
    if (segs[i] === '..') { parts.pop(); continue; }
    parts.push(segs[i]);
  }
  var out = parts.join('/');
  return out.charAt(0) === '.' ? out : './' + out;
}

function __resolve(dep, from) {
  if (dep.charAt(0) !== '.') return dep;                 // bare: react, etc.
  var base = from.slice(0, from.lastIndexOf('/'));
  return __norm(base + '/' + dep);
}

function __cjs(id, fn) { __defs[id] = { kind: 'cjs', fn: fn }; }

// AMD's define is variadic: define(factory), define(deps, factory),
// define(id, factory), define(id, deps, factory). Babel emits the last form here
// because moduleIds is on, so a fixed (deps, fn) signature would bind the module
// id to deps and fail on deps.map.
function define() {
  var a = Array.prototype.slice.call(arguments);
  var fn = a.pop();
  var deps = Array.isArray(a[a.length - 1]) ? a.pop() : [];
  var id = (typeof a[0] === 'string') ? a[0] : __pendingId;
  __defs[id] = { kind: 'amd', deps: deps, fn: fn, id: id };
}

function require(dep, from) {
  var id = __resolve(dep, from || './');
  if (id === './styles.css' || /\\.css$/.test(id)) return {};
  if (__cache[id]) return __cache[id].exports;
  var def = __defs[id];
  if (!def) throw new Error('module not found: ' + id + ' (from ' + from + ')');
  var mod = { exports: {} };
  __cache[id] = mod;
  try {
    if (def.kind === 'cjs') {
      def.fn(function (d) { return require(d, id); }, mod, mod.exports);
    } else {
      var args = def.deps.map(function (d) {
        if (d === 'exports') return mod.exports;
        if (d === 'require') return function (x) { return require(x, id); };
        if (d === 'module') return mod;
        return require(d, id);
      });
      def.fn.apply(null, args);
    }
  } catch (e) {
    // Do not leave a half-initialised module in the cache; it turns the real
    // error into a confusing "element type is invalid" further downstream.
    delete __cache[id];
    throw e;
  }
  return mod.exports;
}

// React needs a production-ish environment flag and a global process shim.
window.process = window.process || { env: { NODE_ENV: 'development' } };

// ── fixture-backed fetch ────────────────────────────────────────────────────
// Reads the same payloads the API returns. POSTs (the demo controls) are
// acknowledged without changing state — this harness checks rendering, not
// behaviour, and pretending otherwise would be the misleading option.
var __fixture = ${esc(fixture)};
window.__renderErrors = [];
window.onerror = function (m, s, l, c, e) { window.__renderErrors.push(String(e && e.stack || m)); };
window.addEventListener('unhandledrejection', function (ev) {
  window.__renderErrors.push('unhandled rejection: ' + (ev.reason && ev.reason.message || ev.reason));
});

window.fetch = function (url, opts) {
  var path = String(url);
  var body = (opts && opts.method === 'POST') ? { ok: true, note: '(render check — no backend)' }
                                              : __fixture[path];
  if (body === undefined) {
    window.__renderErrors.push('fixture miss: ' + path);
    return Promise.resolve({ ok: false, status: 404, text: function () { return Promise.resolve('no fixture for ' + path); } });
  }
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
};
</script>
<script>
${cjsBlocks}
</script>
${modules
  .map(
    (m) => `<script>__pendingId = ${JSON.stringify(m.id)};\n${esc(m.code)}\n</script>`,
  )
  .join('\n')}
<script>
try { require('./main.jsx'); }
catch (e) { window.__renderErrors.push(String(e.stack || e)); document.getElementById('root').textContent = 'LOADER ERROR: ' + e.message; }
</script>
</body></html>
`

mkdirSync(dirname(resolve(OUT)), { recursive: true })
writeFileSync(OUT, html, 'utf8')
console.log(`wrote ${OUT}  (${Math.round(html.length / 1024)} KB)`)
console.log(`  ${modules.length} dashboard modules + ${Object.keys(REACT_MODULES).length} React modules inlined`)
console.log('  open it in a browser; window.__renderErrors should be empty')
