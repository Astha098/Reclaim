/**
 * JSX/JS syntax gate for the dashboard.
 *
 *     node scripts/jsx_check.mjs
 *
 * Not a substitute for `npm run build`. This project was developed in an
 * environment where the npm registry is unreachable, so Vite could not be
 * installed and the dashboard could not be built or rendered here. What this does
 * is eliminate the largest failure class that a reviewer would otherwise hit on
 * first run — a file that does not parse — by running every source file through
 * Babel with the same JSX and ESM settings Vite uses.
 *
 * It also catches two mistakes Babel reports for free and that are easy to make
 * across a dozen small components: a duplicate binding, and an import of a file
 * that is not there.
 *
 * Babel is resolved from wherever it happens to be installed rather than declared
 * as a devDependency, so this script is a local convenience and never something a
 * reviewer has to install to run the real build. It skips itself if Babel is
 * absent.
 */

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, extname, join, resolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(HERE, '..', 'dashboard', 'src')

/**
 * Resolve Babel from a bare import, or from an explicit `BABEL_MODULES` directory.
 *
 * The env var exists because ESM resolution ignores NODE_PATH, so pointing this at
 * a Babel installed somewhere other than a local `node_modules` is otherwise
 * impossible — which is exactly the situation in an environment where the registry
 * is blocked and the only copy of Babel lives in a scratch directory.
 */
async function loadBabel() {
  try {
    return {
      core: await import('@babel/core'),
      preset: (await import('@babel/preset-react')).default,
    }
  } catch {
    /* fall through to the explicit path */
  }
  const dir = process.env.BABEL_MODULES
  if (!dir) return null
  try {
    return {
      core: await import(pathToFileURL(join(dir, '@babel', 'core', 'lib', 'index.js')).href),
      preset: (
        await import(pathToFileURL(join(dir, '@babel', 'preset-react', 'lib', 'index.js')).href)
      ).default,
    }
  } catch {
    return null
  }
}

const babelPkg = await loadBabel()
if (!babelPkg) {
  console.log('skip: @babel/core not installed (npm i -D @babel/core @babel/preset-react)')
  process.exit(0)
}
const babel = babelPkg.core.default ?? babelPkg.core
const presetReact = babelPkg.preset.default ?? babelPkg.preset

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name)
    return statSync(p).isDirectory() ? walk(p) : [p]
  })
}

if (!existsSync(SRC)) {
  console.error(`no such directory: ${SRC}`)
  process.exit(1)
}

const files = walk(SRC).filter((f) => ['.js', '.jsx'].includes(extname(f)))
const failures = []
const localImports = []

for (const file of files) {
  const code = readFileSync(file, 'utf8')
  const rel = file.slice(SRC.length + 1)
  try {
    const out = babel.transformSync(code, {
      filename: file,
      babelrc: false,
      configFile: false,
      presets: [[presetReact, { runtime: 'automatic' }]],
      sourceType: 'module',
    })
    // Collect relative imports so a renamed component surfaces here rather than
    // as a blank screen in the browser.
    for (const m of code.matchAll(/from\s+'(\.[^']+)'/g)) {
      localImports.push([rel, m[1], resolve(dirname(file), m[1])])
    }
    console.log(`  ✓ ${rel}  (${out.code.split('\n').length} lines out)`)
  } catch (e) {
    console.log(`  ✗ ${rel}  — ${e.message.split('\n')[0]}`)
    failures.push(rel)
  }
}

for (const [from, spec, target] of localImports) {
  if (!existsSync(target) && !existsSync(target + '.js') && !existsSync(target + '.jsx')) {
    console.log(`  ✗ ${from} imports '${spec}' which does not exist`)
    failures.push(`${from} → ${spec}`)
  }
}

console.log('─'.repeat(60))
if (failures.length) {
  console.log(`FAILED ${failures.length}/${files.length}: ${failures.join(', ')}`)
  process.exit(1)
}
console.log(`PASSED ${files.length}/${files.length} files parse; all local imports resolve`)
