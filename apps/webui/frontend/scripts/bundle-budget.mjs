// bundle-budget.mjs -- per-surface gzip budgets for the emitted client bundle.
//
// WHY THIS EXISTS IN THIS SHAPE
// The predecessor gate summed every file in build/_app/immutable/chunks/*.js and
// called the total "the library page bundle". That was wrong in both directions:
//   - it COUNTED chunks the library page never downloads (the /performance route
//     DSP alone is about 187 KB gzip, lazily imported, counted in full), and
//   - it MISSED files the library page does download (entry/*.js and nodes/*.js
//     were outside the glob, about 32 KB gzip).
// Measured on c3cf9329 the old gate read 252,219 of 256,000 while the library
// page actually pulled 93,011. It was about to red unrelated PRs at roughly 3.8 KB
// of apparent headroom, for weight that page never fetches.
//
// The repair is to measure per SURFACE, from the real module graph, and to leave
// nothing outside a budget.
//
// HOW A SURFACE IS COMPUTED
// SvelteKit code-splits at every dynamic import. A dynamic import is therefore a
// budget boundary: whatever sits behind one is not paid for by the page that did
// not take that branch. So each surface is the STATIC import closure of its roots:
//   library      roots = every _app/immutable/*.js referenced by index.html,
//                plus the root layout node, plus the node for route "/".
//   performance  roots = the nodes for "/performance" and its children.
//   other-lazy   roots = every remaining route node, plus deferred app-shell
//                chunks such as SvelteKit's error template, which is dynamically
//                imported from entry/app.js and belongs to no route.
// A file is charged to the FIRST surface that reaches it, in that order, so a
// chunk shared between the library page and /performance is charged to the
// library page (which is what actually downloads it on first paint) and is not
// double counted.
//
// RUNTIME-LOADED MODULES
// AudioWorklet processors are emitted as standalone .js and fetched by URL
// (`new URL("../assets/x.js", import.meta.url)`), never imported, so no module
// graph reaches them. They are still real downloads. After the closures are
// computed, any leftover file whose emitted name appears as a literal inside a
// surface's own code is charged to that surface. main already ships one of these
// (the xrun sentinel processor, charged to performance); the color-FX worklet on
// PR #492 will be charged the same way, automatically.
//
// COVERAGE IS THE POINT
// Every .js emitted under _app/immutable/ must land in exactly one budget. A file
// reached by no surface and named by no surface is reported as UNATTRIBUTED and
// fails the run. That is what stops the original bug returning: new lazily-loaded
// weight cannot appear in a directory nobody sums. The inventory walks
// recursively on purpose, so a future workers/ directory is caught, not ignored.
//
// SCOPE: JS only. CSS and other assets are emitted as Vite "assets", not chunks,
// and were outside the predecessor gate too. Widening to CSS would change what
// the 250 KB number means, so it is deliberately left alone. See README.

import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { join, dirname, normalize, relative } from 'node:path';
import { BudgetError, BUDGETS_DIR, chargeBranchId, loadBudgets } from './bundle-budget-load.mjs';

// ---------------------------------------------------------------- budgets ---

// The limits are not in this file. Each surface is budgets/<surface>/base.json (its base limit
// and how chunks are attributed to it) plus budgets/<surface>/charges/<pr>.json, one per PR that
// grew it, and the gate enforces limit = base + sum(charges) (scripts/bundle-budget-load.mjs,
// ADR-NEW-bundle-budget-charges). A PR that grows a surface ADDS a charge file; it never edits a
// base. The dated derivations that used to sit here are in budgets/HISTORY.md and
// budgets/<surface>/HISTORY.md. Marker surfaces such as ai-sidebar and set-notes-editor carry
// their attribution strings in base.json; main's released #7892 sidebar bucket lives under
// budgets/ai-sidebar/, and library first-paint growth from that PR is a charge file.
// PR #8342 tempo curve growth is budgets/other-lazy/charges/8342.json (482304 + 2048 => 484352).
//
// TWO READINGS OF THE SAME NUMBERS (ADR-NEW-bundle-budget-pr-delta, Wed 4 Nov 2026):
//   --baseline <json> --own <ids>   a PULL REQUEST is charged by its own delta: each surface's
//                                   bytes minus the same surface in main's measurement at the
//                                   merge base (`--json --measure` output of a main run), and
//                                   the allowance is the sum of THIS PR's own charge files
//                                   (`--own` names their ids: the PR number and the branch).
//                                   Default allowance 0. The absolute ceiling is printed as a
//                                   warning only. Two concurrent PRs each under the ceiling can
//                                   no longer cross it together and red every later PR.
//   --report-only                   a PUSH TO MAIN measures and publishes; a ceiling crossing
//                                   is a warning, never a red main.
//   (neither)                       the NIGHTLY ratchet: the absolute ceiling, limit = base +
//                                   charges, fails the run, and ci.yml opens a rebaseline issue.
// Coverage (every chunk charged to a surface) fails in every mode.


// Build noise on a PR delta: two builds of the same source differ by a few bytes gzip per surface
// (chunk hashes and import order; 1 to 21 B measured on CI Thu 5 Nov 2026, #8534 run 42043423398).
// A PR's delta passes while it is within its own charges plus this floor, so noise never reds a PR
// and a real feature (hundreds of bytes and up) still needs a charge file.
const PR_DELTA_NOISE_BYTES = 128;

// ---------------------------------------------------------------- helpers ---

// Static specifiers only: `from"x"` and a bare side-effect `import"x"`.
// `import("x")` has a paren before the quote so it deliberately does NOT match:
// dynamic imports are the split points that separate one budget from the next.
const STATIC_IMPORT = /(?:\bfrom|\bimport)\s*["']([^"']+)["']/g;
const DYNAMIC_IMPORT = /\bimport\s*\(\s*["']([^"']+)["']\s*\)/g;

function _fail(message) {
  console.error(`[ERROR] ${message}`);
  process.exit(1);
}

function _readRouteDictionary(kitAppPath) {
  let source;
  try {
    source = readFileSync(kitAppPath, 'utf8');
  } catch {
    _fail(
      `route dictionary not found at ${kitAppPath}. ` +
        'It is written by `svelte-kit sync` during `pnpm build`; run the build first.',
    );
  }
  const match = source.match(/export const dictionary = (\{[\s\S]*?\n\s*\});/);
  if (!match) {
    _fail(`could not parse the route dictionary out of ${kitAppPath} (SvelteKit output shape changed).`);
  }
  try {
    return JSON.parse(match[1]);
  } catch (error) {
    _fail(`route dictionary in ${kitAppPath} is not valid JSON: ${error.message}`);
  }
}

function _listJsFiles(immutableDir) {
  const found = [];
  const walk = (absolute) => {
    for (const entry of readdirSync(absolute)) {
      const next = join(absolute, entry);
      if (statSync(next).isDirectory()) walk(next);
      else if (entry.endsWith('.js')) found.push(relative(immutableDir, next).replace(/\\/g, '/'));
    }
  };
  walk(immutableDir);
  return found.sort();
}

function _specifiers(immutableDir, relativePath, pattern) {
  const source = readFileSync(join(immutableDir, relativePath), 'utf8');
  const out = new Set();
  for (const match of source.matchAll(pattern)) {
    if (!match[1].startsWith('.')) continue;
    out.add(normalize(join(dirname(relativePath), match[1])).replace(/\\/g, '/'));
  }
  return out;
}

function _staticClosure(immutableDir, roots, known) {
  const seen = new Set();
  const queue = roots.filter((r) => known.has(r));
  while (queue.length) {
    const file = queue.pop();
    if (seen.has(file)) continue;
    seen.add(file);
    for (const dep of _specifiers(immutableDir, file, STATIC_IMPORT)) {
      if (known.has(dep) && !seen.has(dep)) queue.push(dep);
    }
  }
  return seen;
}

function _nodeFile(immutableDir, index) {
  const name = readdirSync(join(immutableDir, 'nodes')).find((f) => f.split('.')[0] === String(index));
  if (!name) _fail(`route node ${index} named by the dictionary has no emitted file in nodes/.`);
  return `nodes/${name}`;
}

function _isPerformanceRoute(route) {
  return route === '/performance' || route.startsWith('/performance/');
}

// ------------------------------------------------------------------ main ---

function main() {
  const args = process.argv.slice(2);
  const rootIndex = args.indexOf('--root');
  const root = rootIndex >= 0 ? args[rootIndex + 1] : '.';
  const asJson = args.includes('--json');
  // --budgets points the gate at another budgets/ tree; the unit tests use it for synthetic charges.
  const budgetsIndex = args.indexOf('--budgets');
  const budgetsDir = budgetsIndex >= 0 ? args[budgetsIndex + 1] : BUDGETS_DIR;
  const _value = (flag) => {
    const index = args.indexOf(flag);
    if (index < 0) return null;
    const value = args[index + 1];
    if (value === undefined || value.startsWith('--')) _fail(`${flag} needs a value`);
    return value;
  };
  const baselinePath = _value('--baseline');
  const ownIds = [
    ...new Set(
      (_value('--own') ?? '')
        .split(',')
        .map((id) => id.trim())
        .filter(Boolean)
        .map((id) => chargeBranchId(id))
    )
  ];
  const reportOnly = args.includes('--report-only');
  const measurePath = _value('--measure');
  const sha = _value('--sha');
  if (baselinePath && reportOnly) _fail('--baseline and --report-only are different readings; pass one');
  if (ownIds.length && !baselinePath) _fail('--own names this PR\'s charges and only means something with --baseline');
  let baseline = null;
  if (baselinePath) {
    let parsed;
    try {
      parsed = JSON.parse(readFileSync(baselinePath, 'utf8'));
    } catch (error) {
      _fail(`--baseline ${baselinePath}: cannot read as JSON: ${error.message}`);
    }
    if (!parsed || typeof parsed.totals !== 'object' || parsed.totals === null) {
      _fail(`--baseline ${baselinePath}: no "totals" object; it must be a \`--json\` measurement of main`);
    }
    for (const [name, total] of Object.entries(parsed.totals)) {
      if (!Number.isInteger(total?.bytes) || total.bytes < 0) {
        _fail(`--baseline ${baselinePath}: totals.${name}.bytes is not a non-negative integer`);
      }
    }
    baseline = parsed;
  }
  let BUDGETS;
  try {
    BUDGETS = loadBudgets(budgetsDir);
  } catch (error) {
    if (error instanceof BudgetError) _fail(error.message);
    throw error;
  }

  const buildDir = join(root, 'build');
  const immutableDir = join(buildDir, '_app/immutable');
  const kitAppPath = join(root, '.svelte-kit/generated/client/app.js');

  try {
    if (!statSync(immutableDir).isDirectory()) throw new Error('not a directory');
  } catch {
    _fail(`${immutableDir} not found; run 'pnpm build' first.`);
  }

  const dictionary = _readRouteDictionary(kitAppPath);
  if (!dictionary['/']) _fail('the route dictionary has no "/" entry, so the library page cannot be located.');

  const inventory = _listJsFiles(immutableDir);
  const known = new Set(inventory);

  // Roots.
  const html = readFileSync(join(buildDir, 'index.html'), 'utf8');
  const htmlRoots = [...html.matchAll(/\/_app\/immutable\/([^"')\s]+\.js)/g)].map((m) => m[1]);
  if (htmlRoots.length === 0) _fail('index.html references no _app/immutable JS; the build looks broken.');

  const libraryRoots = [...htmlRoots, _nodeFile(immutableDir, 0), _nodeFile(immutableDir, dictionary['/'][0])];
  const performanceRoots = Object.entries(dictionary)
    .filter(([route]) => _isPerformanceRoute(route))
    .flatMap(([, nodes]) => nodes)
    .map((n) => _nodeFile(immutableDir, n));
  if (performanceRoots.length === 0) {
    _fail('no /performance route found in the dictionary; the performance budget would measure nothing.');
  }

  const libraryFiles = _staticClosure(immutableDir, libraryRoots, known);
  const performanceFiles = _staticClosure(immutableDir, performanceRoots, known);

  // Everything else lazily reachable: remaining route nodes, plus deferred
  // app-shell chunks (SvelteKit's error template is dynamically imported from
  // entry/app.js and belongs to no route).
  const otherRoots = [
    _nodeFile(immutableDir, 1),
    ...Object.entries(dictionary)
      .filter(([route]) => !_isPerformanceRoute(route))
      .flatMap(([, nodes]) => nodes)
      .map((n) => _nodeFile(immutableDir, n)),
  ];
  for (const file of [...libraryFiles, ...performanceFiles]) {
    for (const target of _specifiers(immutableDir, file, DYNAMIC_IMPORT)) {
      if (known.has(target) && !libraryFiles.has(target) && !performanceFiles.has(target)) otherRoots.push(target);
    }
  }
  const otherFiles = _staticClosure(immutableDir, otherRoots, known);

  // A marker surface (the assistant sidebar and Set notes editor, for two) sits dynamic imports
  // below the deferred shell, so each is found by content: every lazy chunk carrying one of its
  // markers is a root. Every marker must be found, or the surface would undercount.
  const lazyText = inventory
    .filter((file) => !libraryFiles.has(file) && !performanceFiles.has(file))
    .map((file) => [file, readFileSync(join(immutableDir, file), 'utf8')]);
  const graphFiles = { library: libraryFiles, performance: performanceFiles, other: otherFiles };
  const reach = {};
  for (const budget of BUDGETS) {
    const { graph, markers } = budget.base.attribution;
    if (graph) {
      reach[budget.name] = graphFiles[graph];
      continue;
    }
    const missing = markers.filter((marker) => !lazyText.some(([, text]) => text.includes(marker)));
    if (missing.length) {
      _fail(`no lazy chunk carries ${missing.join(', ')}; the ${budget.name} budget would undercount.`);
    }
    const roots = lazyText.filter(([, text]) => markers.some((marker) => text.includes(marker))).map(([file]) => file);
    reach[budget.name] = _staticClosure(immutableDir, roots, known);
  }

  // Second pass: runtime-loaded modules (AudioWorklet processors and the like)
  // are fetched by URL, so no import edge reaches them. Charge each to the
  // surface whose own code names it. Anything named by nobody stays
  // unattributed and fails below, which is the intended loud failure.
  const attributedByGraph = new Set(Object.values(reach).flatMap((files) => [...files]));
  const leftovers = inventory.filter((f) => !attributedByGraph.has(f));
  if (leftovers.length) {
    const bodies = Object.fromEntries(
      BUDGETS.map((b) => [
        b.name,
        [...reach[b.name]].map((f) => readFileSync(join(immutableDir, f), 'utf8')).join('\n'),
      ]),
    );
    for (const file of leftovers) {
      const basename = file.split('/').pop();
      const owner = BUDGETS.find((b) => bodies[b.name].includes(basename));
      if (owner) reach[owner.name].add(file);
    }
  }

  const charge = (file) => BUDGETS.find((b) => reach[b.name].has(file))?.name ?? 'UNATTRIBUTED';

  const totals = Object.fromEntries(BUDGETS.map((b) => [b.name, { bytes: 0, files: 0 }]));
  const unattributed = [];
  for (const file of inventory) {
    const bucket = charge(file);
    const bytes = gzipSync(readFileSync(join(immutableDir, file))).length;
    if (bucket === 'UNATTRIBUTED') unattributed.push({ file, bytes });
    else {
      totals[bucket].bytes += bytes;
      totals[bucket].files += 1;
    }
  }

  const measurement = { sha: sha ?? baseline?.sha ?? null, totals, unattributed, budgets: BUDGETS };
  if (measurePath) {
    mkdirSync(dirname(measurePath), { recursive: true });
    writeFileSync(measurePath, JSON.stringify(measurement, null, 2));
  }
  if (asJson) {
    console.log(JSON.stringify(measurement, null, 2));
  } else {
    console.log('bundle budgets (gzip bytes, JS under _app/immutable/):');
    for (const budget of BUDGETS) {
      const { bytes, files } = totals[budget.name];
      const pct = ((bytes / budget.limit) * 100).toFixed(1);
      const verdict = bytes > budget.limit ? 'OVER' : 'ok';
      console.log(
        `  ${budget.name.padEnd(12)} ${String(bytes).padStart(7)} / ${String(budget.limit).padStart(6)}` +
          `  ${pct.padStart(5)}%  ${String(files).padStart(2)} files  [${verdict}]  ${budget.note}`,
      );
      const ids = budget.charges.map((charge) => charge.id).join(', ');
      console.log(
        `    limit ${budget.limit} = base ${budget.base.limit} + ${budget.charges.length} charge(s) ${budget.charged}` +
          (ids ? ` (${ids})` : ''),
      );
    }
    const counted = Object.values(totals).reduce((a, t) => a + t.files, 0);
    console.log(`  coverage: ${counted} of ${inventory.length} emitted JS files charged to a budget`);
  }

  // Fail loudly: an unmeasured chunk is the exact bug this gate was rewritten to
  // stop, so it is an error and not a warning.
  let failed = false;
  if (unattributed.length) {
    failed = true;
    console.error(
      `[ERROR] ${unattributed.length} emitted chunk(s) belong to no budget. Every client chunk must be ` +
        'measured by exactly one surface. Add the owning route to a budget, or add a new budget:',
    );
    for (const { file, bytes } of unattributed) console.error(`  ${file} (${bytes} bytes gzip)`);
  }
  for (const budget of BUDGETS) {
    const { bytes } = totals[budget.name];
    const over = bytes > budget.limit;
    if (baseline) {
      // A pull request: its own delta against main at the merge base, within its own charges.
      const before = baseline.totals[budget.name];
      const own = budget.charges.filter((charge) => ownIds.includes(charge.id));
      const allowance = own.reduce((sum, charge) => sum + charge.bytes, 0);
      if (before === undefined) {
        // A surface this PR introduces: nothing to diff against, so its base limit is the allowance.
        if (!asJson) console.log(`  ${budget.name}: new surface, no baseline; read against its own limit ${budget.limit}`);
        if (over) {
          failed = true;
          console.error(
            `[ERROR] new budget "${budget.name}" exceeded: ${bytes} bytes gzip against its ${budget.limit} limit ` +
              `(a new surface's first limit follows the rebaseline rule, budgets/README.md).`,
          );
        }
        continue;
      }
      const delta = bytes - before.bytes;
      const overDelta = delta > allowance + PR_DELTA_NOISE_BYTES;
      const verdict = overDelta ? 'OVER' : 'ok';
      if (!asJson) {
        console.log(
          `  ${budget.name.padEnd(12)} main ${String(before.bytes).padStart(7)} -> ${String(bytes).padStart(7)}` +
            `  delta ${String(delta).padStart(6)}  allowance ${String(allowance).padStart(5)}` +
            (own.length ? ` (${own.map((c) => c.id).join(', ')})` : '') +
            `  [${verdict}]`,
        );
      }
      if (overDelta) {
        failed = true;
        console.error(
          `[ERROR] budget "${budget.name}" grows by ${delta} bytes gzip over main (${before.bytes} -> ${bytes}) ` +
            `against this PR's allowance of ${allowance} (plus ${PR_DELTA_NOISE_BYTES} B build noise)` +
            (own.length ? ` (${own.map((c) => c.id).join(', ')})` : ' (no charge file of its own)') +
            `. If this PR grows the surface on purpose, add budgets/${budget.name}/charges/<pr>.json ` +
            `for the growth (budgets/README.md); main's own size is a nightly ratchet, not this PR's.`,
        );
      }
      if (over) {
        console.log(
          `::warning title=bundle budget ceiling::"${budget.name}" is ${bytes} against the ${budget.limit} ceiling ` +
            `(main ${before.bytes}); the nightly ratchet reports it, this PR is judged on its delta.`,
        );
      }
      continue;
    }
    if (over && reportOnly) {
      console.log(
        `::warning title=bundle budget ceiling::"${budget.name}" is ${bytes} against the ${budget.limit} ceiling ` +
          `(base ${budget.base.limit} + charges ${budget.charged}); report only on a push to main, the nightly ratchet decides.`,
      );
      continue;
    }
    if (over) {
      failed = true;
      console.error(
        `[ERROR] budget "${budget.name}" exceeded: ${bytes} bytes gzip against a ${budget.limit} limit, ` +
          `over by ${bytes - budget.limit} bytes (${budget.note}); limit ${budget.limit} = base ` +
          `${budget.base.limit} + charges ${budget.charged}. If a PR grows the surface on purpose, ` +
          `add budgets/${budget.name}/charges/<pr>.json for the growth; on main this is a rebaseline ` +
          `(budgets/README.md). A PR run is judged on its own delta with --baseline.`,
      );
    }
  }
  if (failed) {
    console.error(
      '[ERROR] reproduce locally: cd apps/webui/frontend && pnpm build && bash scripts/check-bundle-size.sh',
    );
    process.exit(1);
  }
  console.log('[OK] all bundle budgets satisfied and every emitted chunk is measured.');
}

main();
