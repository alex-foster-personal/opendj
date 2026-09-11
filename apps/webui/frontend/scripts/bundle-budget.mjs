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

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { join, dirname, normalize, relative } from 'node:path';

// ---------------------------------------------------------------- budgets ---

// Each limit is a real measurement, not a round number picked for comfort.
// Baseline build: c3cf9329 (main), Mon 31 Aug 2026, `pnpm build`.
//
// library      256,000 bytes. UNCHANGED from the original gate. This is a bug
//              fix to what gets counted, not a raise. Measured 93,011 (36.3%).
//              The generous remaining headroom is inherited, not newly granted.
// performance  measured 193,544, +5% headroom, rounded up to the next KiB
//              => 203,776 (199 KiB). A ratchet at today's number: the route
//              carries the app's real weight and may not silently grow. The
//              measurement includes the xrun sentinel AudioWorklet (1,636),
//              which the predecessor gate never counted at all.
//              RAISED Sat 5 Sep 2026 (PR #1288, pin 93c82bb36eb7): the output
//              liveness health bar under master volume adds a real, permanent
//              part of the always-rendered performance UI (audio-output-health
//              .svelte.ts, audio-output-health-display.ts, the liveness
//              onSnapshot hook) - genuine new weight, not padding. Clean
//              origin/main (54a6220cc) already measured 204,275 of 204,800
//              (525 bytes headroom) before this PR landed, so this was going
//              to trip on the next few bytes of ordinary feature work
//              regardless. With this PR: measured 205,141, +5% ceil-to-KiB
//              => 216,064 (211 KiB).
//              RAISED Thu 10 Sep 2026 (PR #1010, pin 593b23ffe): the
//              discussion_r3975326238 fix threads a new isSuperseded() guard
//              through refreshAnalysisSourceDecks/_refreshDecks/_adopt in
//              analysis-source.svelte.ts and analysis-source-refresh.ts,
//              plus the JSDoc explaining the race it closes. CI measured
//              216,952 on this head (deterministic across two independent
//              CI builds); the equivalent local `pnpm build` on the exact
//              same pinned toolchain (Node 22.14.0, pnpm 11.9.0, svelte
//              5.56.7, @sveltejs/kit 2.70.1, vite 5.4.21, terser 5.51.2 -
//              all confirmed byte-for-byte against CI's own install log)
//              measured only 214,470 for the identical source tree - a
//              pre-existing local-vs-CI gap (214,450 vs 214,747, ~300
//              bytes) that grew to ~2,500 bytes specifically on this diff.
//              Isolated with `pnpm build` chunk tables from both sides: the
//              entire delta sits in the one large shared chunk carrying
//              this route's bulk (HUoDj3ab.js on CI / I_4JPHqH.js locally),
//              which is exactly the chunk these edited files land in -
//              consistent with terser's frequency-based mangling producing
//              a slightly different (non-hermetic) minification result
//              when Rollup's module concatenation order shifts with
//              filesystem enumeration order between the two build
//              environments, not with new dead weight. Ratcheting on CI's
//              own number since that is what the gate actually runs
//              against: 216,952, +5% ceil-to-KiB => 228,352 (223 KiB).
//              ALSO RAISED Thu 10 Sep 2026 (PR #1587, merged into main
//              independently): CI (`frontend unit + check + build`, run
//              34458533006, job 102810569554) measured 216,124 on main's
//              tip alone, 60 bytes over the prior 216,064 ceiling, from
//              unrelated feature work plus the same local-vs-CI build gap
//              documented above. That PR's own ratchet, 227,328 (222 KiB),
//              is superseded here by this merge because PR #1010's ceiling
//              (228,352) is the larger of the two and covers both diffs'
//              combined weight; re-measure on the merged head before
//              tightening either number back down.
// other-lazy   measured 60,160, same +5% ceil-to-KiB rule => 63,488 (62 KiB).
//              RAISED Mon 1 Sep 2026: the CloudSync config route (/cloudsync
//              policy matrix, machines, pins, overview + api-cloudsync client)
//              is a genuine 17th route in this shared bucket; new measured
//              64,328, same +5% ceil-to-KiB rule => 67,584 (66 KiB). Widened
//              deliberately per the rule above, not by accident.
//              This surface exists so that no chunk is unmeasured. It spans 16
//              routes plus the error template, so its 3 KB of headroom is TIGHT
//              by construction; widen it deliberately if ordinary feature work
//              starts tripping it, but widen it visibly rather than by accident.
//
// The +5% rule applies to the two ratcheted budgets (performance, other-lazy)
// so neither carries slack invented case by case; library keeps its inherited
// ceiling as documented above. Raising any number below is a deliberate,
// reviewable act.
const BUDGETS = [
  { name: 'library', limit: 256000, measured: 93011, note: 'initial load of "/"' },
  // Wed 2 Sep 2026 18:40: +1 KiB for audio-output-liveness (P0: "no audio" must be an error
  // state; main had 24 bytes of headroom). Payback: PR #695 ships signalsmith-stretch once.
  // Thu 10 Sep 2026: +12 KiB for the isSuperseded() supersession-guard fix
  // (PR #1010, discussion_r3975326238). CI measured 216,952 on this diff.
  // Merge note: PR #1587 (main) independently measured 216,124 with its own
  // unrelated changes and a smaller ratchet (227,328); PR #1010's larger
  // ceiling is kept since it is the wider of the two and both diffs are now
  // present on this branch. Re-measure on the merged head before tightening.
  { name: 'performance', limit: 228352, measured: 216952, note: '/performance and children' },
  // Thu 10 Sep 2026: 66 -> 108 KiB for Q18 rung 1 (PR #1691). `@wasm-audio-decoders/flac`
  // is dynamically imported, so it lands here rather than in the deck route's eager
  // closure - measured as ONE chunk of 43833 gzip bytes, which is the whole of the
  // increase (107319 total, 63486 without it, against main's 64328). It is fetched only
  // when a stemmed deck loads, never at boot and never on a route that has no stems, so
  // it costs nothing on the boot-latency budget this program is elsewhere reducing.
  // Flagged rather than absorbed quietly: the PR's own body measured the deck route
  // closure (+2 KB) and did NOT measure this bucket, so this cost was unreported until
  // the gate caught it. If the AAC rung (Q18 rung 2) ever replaces this decoder rather
  // than adding to it, this limit comes back down with it. Merge note (PR #1010 x
  // #1691): PR #1010 touches only the `performance` surface, so this `other-lazy`
  // ceiling and its measured figure are carried from main unchanged.
  // RAISED Fri 11 Sep 2026: the Library Wheel route (/library-wheel SVG sunburst,
  // axis picker, fail-fast wheel-api client) is a new lazy SvelteKit node in this
  // shared bucket; measured 113,342, same +5% ceil-to-KiB rule => 119,808 (117 KiB).
  { name: 'other-lazy', limit: 119808, measured: 113342, note: 'all other routes plus deferred shell' },
];

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

  const reach = { library: libraryFiles, performance: performanceFiles, 'other-lazy': otherFiles };

  // Second pass: runtime-loaded modules (AudioWorklet processors and the like)
  // are fetched by URL, so no import edge reaches them. Charge each to the
  // surface whose own code names it. Anything named by nobody stays
  // unattributed and fails below, which is the intended loud failure.
  const attributedByGraph = new Set([...libraryFiles, ...performanceFiles, ...otherFiles]);
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

  if (asJson) {
    console.log(JSON.stringify({ totals, unattributed, budgets: BUDGETS }, null, 2));
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
    if (bytes > budget.limit) {
      failed = true;
      console.error(
        `[ERROR] budget "${budget.name}" exceeded: ${bytes} bytes gzip against a ${budget.limit} limit, ` +
          `over by ${bytes - budget.limit} bytes (${budget.note}).`,
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
