// Mutation tests for the client bundle budget gate (scripts/bundle-budget.mjs).
//
// A CI gate that cannot fail is not a gate. The predecessor here summed every
// file in build/_app/immutable/chunks/*.js and called it "the library page
// bundle": it counted about 187 KB of /performance DSP the library page never
// downloads, and missed the entry/ and nodes/ files it does. Nobody noticed for
// five weeks because nothing ever constructed a state that should trip it.
//
// So every budget is proven to FAIL here, on a synthetic SvelteKit output tree
// built in a temp dir, with no dependency on a real `pnpm build`. These run in
// `pnpm test:unit`, which CI already invokes.
//
// Regression lines:
// - if a base limit in budgets/<surface>/base.json moves without this file's EXPECTED_BASE
//   moving with it then a base was edited outside a rebaseline: growth is a charge file
//   (budgets/<surface>/charges/<pr>.json), never a base edit (ADR-NEW-bundle-budget-charges)
// - if the enforced limit stops being base + sum(charges) then a charge is decorative, or two
//   PRs' charges no longer add under merge skew
// - if a malformed, padded or stray charge file stops failing then a charge nobody checks can
//   widen a budget
// - if a surface budget stops failing when its own chunk grows past the limit
//   then that budget is decorative
// - if an emitted chunk reachable from nothing stops failing the run then the
//   original bug is back: new weight can hide in a directory nobody sums
// - if a URL-loaded AudioWorklet stops being charged to the surface that names
//   it then worklet weight is invisible again
// - if the failure message stops naming the budget and the overage then a red
//   build cannot be diagnosed from its log

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { after, test } from 'node:test';
import { loadBudgets } from '../../scripts/bundle-budget-load.mjs';

const GATE = fileURLToPath(new URL('../../scripts/bundle-budget.mjs', import.meta.url));
const roots = [];
after(() => roots.forEach((r) => rmSync(r, { recursive: true, force: true })));

// ---------------------------------------------------------------- fixture ---

// Deterministic, effectively incompressible padding, so a fixture can be grown
// to a known GZIP size. The budgets are gzip budgets, so the tests have to move
// the gzip needle rather than the raw byte count. base64 of random bytes is
// about 4/3 the length and gzips to about 3/4 of that, so the gzip size lands
// near the random byte count: size it once and top up, rather than re-gzipping
// a growing string (which made this file take 40s).
function _pad(targetGzip, seed) {
  let state = seed;
  const next = () => ((state = (state * 1103515245 + 12345) & 0x7fffffff) >>> 8) & 0xff;
  const fill = (n) => {
    const buffer = Buffer.alloc(n);
    for (let i = 0; i < n; i += 1) buffer[i] = next();
    return buffer;
  };
  const parts = [fill(Math.ceil(targetGzip * 1.05) + 64)];
  const render = () => `const _p="${Buffer.concat(parts).toString('base64')}";`;
  let body = render();
  while (gzipSync(Buffer.from(body)).length < targetGzip) {
    parts.push(fill(Math.ceil(targetGzip * 0.1) + 64));
    body = render();
  }
  return body;
}

// A miniature but structurally faithful SvelteKit static build:
// index.html + entry/ + nodes/ + chunks/, plus the generated route dictionary.
// `sizes` inflates one chunk per surface; `extras` injects mutation cases.
function _fixture({ sizes = {}, extras = {} } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'bundle-budget-'));
  roots.push(root);
  const imm = join(root, 'build/_app/immutable');
  for (const dir of ['entry', 'nodes', 'chunks', 'assets']) mkdirSync(join(imm, dir), { recursive: true });
  mkdirSync(join(root, '.svelte-kit/generated/client'), { recursive: true });

  const write = (rel, body) => writeFileSync(join(imm, rel), body);

  // Shared + per-surface chunks. Each surface owns one chunk that can be grown.
  write('chunks/shared.js', 'export const s=1;');
  write('chunks/lib.js', `import"./shared.js";export const l=1;${_pad(sizes.library ?? 64, 1)}`);
  write('chunks/perf.js', `import"./shared.js";export const p=1;${_pad(sizes.performance ?? 64, 2)}`);
  write('chunks/other.js', `import"./shared.js";export const o=1;${_pad(sizes['other-lazy'] ?? 64, 3)}`);
  // The assistant sidebar: two lazy chunks from the root layout, each found by its own literal.
  write('chunks/sidebar.js', `export const a="/api/v1/assistant/agent";${_pad(sizes['ai-sidebar'] ?? 64, 4)}`);
  write('chunks/mount.js', `export const t="Open the assistant";import("./sidebar.js");${_pad(sizes.mount ?? 64, 5)}`);
  // The Set notes editor: lazy from the root layout, found by its ProseMirror class name.
  write('chunks/editor.js', `export const e="ProseMirror-focused";${_pad(sizes['set-notes-editor'] ?? 64, 4)}`);
  // The waveform quick-settings popover: lazy from /performance, found by its "All settings" title.
  write(
    'chunks/wqs.js',
    `export const q="Open every setting in the Settings overlay";${_pad(sizes['wave-quick-settings'] ?? 64, 5)}`,
  );
  // ARTTHUMB-8728d: the /playlists card grid route, found by its heading title.
  write('chunks/plgrid.js', `export const g="Every playlist as a card with its art";${_pad(sizes['playlists-grid'] ?? 64, 6)}`);
  // SKINLAYOUT-8869: the three-panel library extras, found by their data-surface marker.
  write('chunks/threepanel.js', `export const t="three-panel library layout";${_pad(sizes['three-panel-layout'] ?? 64, 7)}`);
  // SKIN-8871B: the /practice/<device> replica route, found by its resolver's error text.
  write('chunks/practice.js', `export const p="neither binds nor hints";${_pad(sizes['practice-replica'] ?? 64, 7)}`);
  write('chunks/error-template.js', 'export default()=>"err";');

  write('entry/start.AAAAAAAA.js', 'import"../chunks/shared.js";');
  // The app shell dynamically imports every node and the error template. Dynamic
  // imports are budget boundaries, so these must NOT pull the whole app into the
  // library surface: that distinction is itself under test via the totals.
  write(
    'entry/app.BBBBBBBB.js',
    'import"../chunks/shared.js";' +
      ['0', '1', '2', '3', '6'].map((n) => `import("../nodes/${n}.CCCCCCCC.js");`).join('') +
      'import("../chunks/error-template.js");',
  );

  write('nodes/0.CCCCCCCC.js', 'import"../chunks/shared.js";import("../chunks/mount.js");import("../chunks/editor.js");'); // root layout
  write('nodes/1.CCCCCCCC.js', 'import"../chunks/shared.js";'); // error node
  write('nodes/2.CCCCCCCC.js', 'import"../chunks/lib.js";'); // route "/"
  write('nodes/3.CCCCCCCC.js', 'import"../chunks/other.js";import("../chunks/plgrid.js");import("../chunks/practice.js");import("../chunks/threepanel.js");'); // route "/sets"
  write(`nodes/6.CCCCCCCC.js`, `import"../chunks/perf.js";import("../chunks/wqs.js");${extras.perfBody ?? ''}`); // "/performance"

  for (const [rel, body] of Object.entries(extras.files ?? {})) write(rel, body);

  writeFileSync(
    join(root, 'build/index.html'),
    ['entry/start.AAAAAAAA.js', 'entry/app.BBBBBBBB.js', 'nodes/0.CCCCCCCC.js', 'chunks/shared.js']
      .map((f) => `<link href="/_app/immutable/${f}" rel="modulepreload">`)
      .join('\n'),
  );
  writeFileSync(
    join(root, '.svelte-kit/generated/client/app.js'),
    'export const dictionary = {\n' +
      '\t\t"/": [2],\n\t\t"/sets": [3],\n\t\t"/performance": [6]\n\t};\n',
  );
  return root;
}

function _run(root, budgetsDir) {
  const extra = budgetsDir ? ['--budgets', budgetsDir] : [];
  try {
    const stdout = execFileSync('node', [GATE, '--root', root, ...extra], { encoding: 'utf8', stdio: 'pipe' });
    return { code: 0, out: stdout };
  } catch (error) {
    return { code: error.status, out: `${error.stdout ?? ''}${error.stderr ?? ''}` };
  }
}

// ------------------------------------------------------------------ tests ---

// The base limits, each rewritten only by a rebaseline on main (measured, ceiled to KiB,
// plus 1 KiB). They were the bundle-budget.mjs limits on main 7837e09617, Tue 3 Nov 2026,
// carried over unchanged; the dated raises behind them are in budgets/<surface>/HISTORY.md.
const EXPECTED_BASE = {
  // Rebaselined Thu 5 Nov 2026 (preventive, main 20a77a0b3e, CI measured 324631 B, 99.4% of its ceiling): measured plus 2%, ceiled to KiB.
  library: 331776,
  // Rebaselined Thu 5 Nov 2026 (preventive, main 86084188b4, CI measured 399251 B, 98.7% of its ceiling): measured plus 2%, ceiled to KiB.
  performance: 407552,
  'set-notes-editor': 81920,
  'ai-sidebar': 36864,
  'wave-quick-settings': 19456,
  // ARTTHUMB-8728d (issue #8592): /playlists card grid, measured 1042 B Wed 4 Nov 2026, ceiled to KiB plus 1 KiB.
  'playlists-grid': 3072,
  // SKINLAYOUT-8869: Now Playing panel and Playing on strip, measured 15762 B Thu 5 Nov 2026 (with the shared chunks it reaches first), ceiled to KiB plus 1 KiB.
  'three-panel-layout': 17408,
  // SKIN-8871B: /practice/<device> replica route, measured 15986 B Thu 5 Nov 2026, ceiled to KiB plus 1 KiB.
  'practice-replica': 17408,
  // Rebaselined Thu 5 Nov 2026 (preventive, main 86084188b4, CI measured 485775 B, 97.2% of its ceiling): measured plus 2%, ceiled to KiB.
  'other-lazy': 495616,
};

test('every base limit is pinned, and every surface on disk has a pin', () => {
  const budgets = loadBudgets();
  assert.deepEqual(
    Object.fromEntries(budgets.map((b) => [b.name, b.base.limit])),
    EXPECTED_BASE,
    'a base moves only on a rebaseline, with this pin; growth is a charge file',
  );
});

test('every surface enforces base plus the sum of its charges, and every charge is well formed', () => {
  for (const budget of loadBudgets()) {
    const sum = budget.charges.reduce((total, charge) => total + charge.bytes, 0);
    assert.equal(budget.limit, budget.base.limit + sum, budget.name);
    for (const charge of budget.charges) {
      assert.match(charge.sha, /^[0-9a-f]{7,40}$/, `${budget.name}/${charge.id}`);
      assert.ok(charge.measured_with_pr > charge.measured_main, `${budget.name}/${charge.id}`);
    }
  }
});

// ------------------------------------------------- synthetic budgets/ tree ---

const SHA = '7837e0961708e8b7a957258dbbcdbfdc3660a07a';

// Small synthetic limits, so a fixture surface can be pushed over or under them cheaply.
function _budgets({ charges = {}, extra = {} } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'bundle-budget-dir-'));
  roots.push(dir);
  const surfaces = {
    library: { order: 10, attribution: { graph: 'library' }, limit: 20480 },
    performance: { order: 20, attribution: { graph: 'performance' }, limit: 20480 },
    'set-notes-editor': { order: 30, attribution: { markers: ['ProseMirror-focused'] }, limit: 20480 },
    'ai-sidebar': {
      order: 40,
      attribution: { markers: ['/api/v1/assistant/agent', 'Open the assistant'] },
      limit: 20480,
    },
    'other-lazy': { order: 90, attribution: { graph: 'other' }, limit: 20480 },
  };
  for (const [name, spec] of Object.entries(surfaces)) {
    mkdirSync(join(dir, name, 'charges'), { recursive: true });
    const base = { note: name, ...spec, measured: 1000, measured_on: 'Tue 3 Nov 2026', baseline_sha: SHA };
    writeFileSync(join(dir, name, 'base.json'), JSON.stringify(base));
  }
  for (const [rel, body] of Object.entries(charges)) {
    writeFileSync(join(dir, rel), typeof body === 'string' ? body : JSON.stringify(body));
  }
  for (const [rel, body] of Object.entries(extra)) writeFileSync(join(dir, rel), body);
  return dir;
}

function _charge(overrides = {}) {
  return {
    bytes: 3000,
    measured_main: 20000,
    measured_with_pr: 22500,
    sha: SHA,
    date: 'Tue 3 Nov 2026',
    reason: 'synthetic growth',
    payback: 'none planned',
    ...overrides,
  };
}

test('a surface over its base fails, and a charge for the growth makes it pass with the arithmetic printed', () => {
  const build = _fixture({ sizes: { library: 22000 } });
  const over = _run(build, _budgets());
  assert.equal(over.code, 1, over.out);
  assert.match(over.out, /budget "library" exceeded/);
  assert.match(over.out, /limit 20480 = base 20480 \+ charges 0/);
  assert.match(over.out, /add budgets\/library\/charges\/<pr>\.json/);

  const charged = _run(build, _budgets({ charges: { 'library/charges/8001.json': _charge() } }));
  assert.equal(charged.code, 0, charged.out);
  assert.match(charged.out, /limit 23480 = base 20480 \+ 1 charge\(s\) 3000 \(8001\)/);
});

test('two PRs charging the same surface add up, so neither races the other on one number', () => {
  const build = _fixture({ sizes: { library: 25000 } });
  const one = _run(build, _budgets({ charges: { 'library/charges/8001.json': _charge() } }));
  assert.equal(one.code, 1, 'one charge alone must not cover both PRs');
  const both = _run(
    build,
    _budgets({
      charges: {
        'library/charges/8001.json': _charge(),
        'library/charges/8002.json': _charge({ bytes: 2500, measured_main: 22500, measured_with_pr: 24600 }),
      },
    }),
  );
  assert.equal(both.code, 0, both.out);
  assert.match(both.out, /limit 25980 = base 20480 \+ 2 charge\(s\) 5500 \(8001, 8002\)/);
});

for (const [label, rel, body, message] of [
  ['a charge with no sha', 'library/charges/8001.json', _charge({ sha: undefined }), /keys must be exactly/],
  ['a charge whose weekday disagrees with its date', 'library/charges/8001.json', _charge({ date: 'Mon 3 Nov 2026' }), /is a Tue, wrong weekday/],
  ['a charge padded past its measured growth', 'library/charges/8001.json', _charge({ bytes: 5000 }), /exceeds the measured growth 2500 ceiled to KiB plus 1 KiB \(4096\)/],
  ['a charge for a PR that did not grow the surface', 'library/charges/8001.json', _charge({ measured_with_pr: 20000 }), /needs no charge/],
  ['a charge with an unknown key', 'library/charges/8001.json', { ..._charge(), limit: 999999 }, /unknown limit/],
  ['a charge that is not JSON', 'library/charges/8001.json', '{bytes: 3000', /not valid JSON/],
  ['a charge file with the wrong name', 'library/charges/PR 8001.txt', _charge(), /not a charge file/],
  ['a stray file in a surface directory', 'library/limit.json', '{}', /is not read by the gate/],
]) {
  test(`${label} FAILS the gate rather than being skipped`, () => {
    const { code, out } = _run(_fixture(), _budgets({ charges: { [rel]: body } }));
    assert.equal(code, 1, `expected a non-zero exit\n${out}`);
    assert.match(out, message);
  });
}

test('a marker surface ordered after other-lazy fails, since it would measure nothing', () => {
  const dir = _budgets();
  const base = JSON.parse(readFileSync(join(dir, 'set-notes-editor', 'base.json'), 'utf8'));
  writeFileSync(join(dir, 'set-notes-editor', 'base.json'), JSON.stringify({ ...base, order: 95 }));
  const { code, out } = _run(_fixture(), dir);
  assert.equal(code, 1, out);
  assert.match(out, /surface order must be library, performance, marker surfaces, then other/);
});

test('a clean build passes and every emitted chunk is charged to a budget', () => {
  const { code, out } = _run(_fixture());
  assert.equal(code, 0, out);
  assert.match(out, /all bundle budgets satisfied/);
  assert.match(out, /coverage: 19 of 19 emitted JS files charged to a budget/);
});

test('dynamic imports are budget boundaries, so the shell does not pull every route into library', () => {
  const { out } = _run(_fixture({ sizes: { performance: 40000 } }));
  // If dynamic imports were followed, the 40 KB performance chunk would be
  // charged to library. It must not be.
  const library = Number(out.match(/library\s+(\d+) \//)[1]);
  const performance = Number(out.match(/performance\s+(\d+) \//)[1]);
  assert.ok(library < 5000, `library should stay small, got ${library}`);
	assert.ok(performance > 39000, `performance should carry the weight, got ${performance}`);
});

test('a dynamic import from performance is charged to other-lazy, not the route entry', () => {
	const { code, out } = _run(
		_fixture({
			extras: {
				files: { 'chunks/perf-deferred.js': _pad(10000, 11) },
				perfBody: 'import("../chunks/perf-deferred.js");'
			}
		})
	);
	assert.equal(code, 0, out);
	const performance = Number(out.match(/performance\s+(\d+) \//)[1]);
	const otherLazy = Number(out.match(/other-lazy\s+(\d+) \//)[1]);
	assert.ok(performance < 5000, `performance must not pay for its deferred chunk, got ${performance}`);
	assert.ok(otherLazy > 9000, `other-lazy must pay for the deferred chunk, got ${otherLazy}`);
});

/**
 * Read a budget's enforced limit (base plus charges) rather than restating it here.
 *
 * The `other-lazy` overflow below used to be the literal 70000, chosen to clear
 * a 67584 limit. When that limit moved to 110592 for the Q18 FLAC decoder
 * (PR #1691), 70000 stopped overflowing anything and this guard went GREEN
 * while asserting that a budget fails - the exact "decorative budget" failure
 * the header above says these tests exist to prevent, one level up. So the
 * number is derived, from the same loader the gate uses, and cannot go stale.
 */
function _limitOf(name) {
  const budget = loadBudgets().find((b) => b.name === name);
  assert.ok(budget, `no ${name} surface under budgets/`);
  return budget.limit;
}

for (const surface of loadBudgets().map((b) => b.name)) {
  // EVERY surface derives from the gate, none is a literal. A literal goes
  // stale the moment a ceiling is raised and then asserts a failure that can
  // no longer happen: when PR #1587 raised performance to 227,328, the pinned
  // `performance: 210000` stopped overflowing and this case passed while
  // proving nothing. That branch re-pinned it to 228000, which is the same
  // maintenance recurring rather than the defect ending. It was written
  // literal on the theory that the fixture's other chunks make the margin
  // unpredictable, but those chunks only ADD to the surface, so limit + 8000
  // always overflows - the theory was right about the exact overage and wrong
  // about the direction, which is the half that matters. Same defect fixed
  // for `other-lazy` one round earlier on this PR; these are its siblings.
  const overflow = _limitOf(surface) + 8000;
  test(`budget "${surface}" FAILS when its own weight exceeds the limit`, () => {
    const { code, out } = _run(_fixture({ sizes: { [surface]: overflow } }));
    assert.equal(code, 1, `expected a non-zero exit\n${out}`);
    assert.match(out, new RegExp(`budget "${surface}" exceeded`));
    assert.match(out, /over by \d+ bytes/, 'the message must state the overage');
  });
}

test('the sidebar mount is charged to ai-sidebar, not to the shared other-lazy ceiling', () => {
  const { code, out } = _run(_fixture({ sizes: { mount: _limitOf('ai-sidebar') + 8000 } }));
  assert.equal(code, 1, `expected a non-zero exit\n${out}`);
  assert.match(out, /budget "ai-sidebar" exceeded/);
  assert.doesNotMatch(out, /budget "other-lazy" exceeded/);
});

test('a sidebar chunk its marker no longer finds FAILS the run rather than undercounting', () => {
  const renamed = { 'chunks/mount.js': 'export const t="renamed";import("./sidebar.js");' };
  const { code, out } = _run(_fixture({ extras: { files: renamed } }));
  assert.equal(code, 1, `expected a non-zero exit\n${out}`);
  assert.match(out, /the ai-sidebar budget would undercount/);
});

test('an emitted chunk that no surface reaches FAILS the run rather than being ignored', () => {
  const { code, out } = _run(_fixture({ extras: { files: { 'chunks/orphan.js': 'export const z=1;' } } }));
  assert.equal(code, 1, `expected a non-zero exit\n${out}`);
  assert.match(out, /belong to no budget/);
  assert.match(out, /chunks\/orphan\.js/);
});

test('a URL-loaded AudioWorklet is charged to the surface whose code names it', () => {
  const worklet = 'assets/fx-processor.DDDDDDDD.js';
  const clean = _run(
    _fixture({
      extras: {
        files: { [worklet]: `export const w=1;${_pad(600, 9)}` },
        perfBody: `const u=new URL("../${worklet}",import.meta.url).href;export{u};`,
      },
    }),
  );
  assert.equal(clean.code, 0, clean.out);
  assert.match(clean.out, /coverage: 20 of 20 emitted JS files charged to a budget/);

  // The same worklet with nobody naming it must fail, so attribution is real
  // and not a blanket pass for anything sitting in assets/.
  const orphaned = _run(_fixture({ extras: { files: { [worklet]: 'export const w=1;' } } }));
  assert.equal(orphaned.code, 1, orphaned.out);
  assert.match(orphaned.out, /belong to no budget/);
});

test('a missing build fails loudly instead of passing vacuously', () => {
  const root = mkdtempSync(join(tmpdir(), 'bundle-budget-empty-'));
  roots.push(root);
  const { code, out } = _run(root);
  assert.equal(code, 1);
  assert.match(out, /not found; run 'pnpm build' first/);
});

test('WAVEQS-8348d the quick-settings popover chunk is charged to its own wave-quick-settings bucket, not other-lazy', () => {
  const { code, out } = _run(_fixture({ sizes: { 'wave-quick-settings': _limitOf('wave-quick-settings') + 8000 } }));
  assert.equal(code, 1, `expected a non-zero exit\n${out}`);
  assert.match(out, /budget "wave-quick-settings" exceeded/);
  assert.doesNotMatch(out, /budget "other-lazy" exceeded/);
});

test('SKINLAYOUT-8869 the three-panel extras are charged to their own three-panel-layout bucket, not other-lazy', () => {
  const { code, out } = _run(_fixture({ sizes: { 'three-panel-layout': _limitOf('three-panel-layout') + 8000 } }));
  assert.equal(code, 1, `expected a non-zero exit\n${out}`);
  assert.match(out, /budget "three-panel-layout" exceeded/);
  assert.doesNotMatch(out, /budget "other-lazy" exceeded/);
});

// ------------------------------------------- PR delta vs main (ADR-NEW-bundle-budget-pr-delta) ---

function _runArgs(root, budgetsDir, extra) {
  try {
    const stdout = execFileSync('node', [GATE, '--root', root, '--budgets', budgetsDir, ...extra], {
      encoding: 'utf8',
      stdio: 'pipe',
    });
    return { code: 0, out: stdout };
  } catch (error) {
    return { code: error.status, out: `${error.stdout ?? ''}${error.stderr ?? ''}` };
  }
}

function _measure(root, budgetsDir, sha) {
  const file = join(mkdtempSync(join(tmpdir(), 'bundle-measure-')), 'measure.json');
  const done = _runArgs(root, budgetsDir, ['--report-only', '--measure', file, '--sha', sha]);
  assert.equal(done.code, 0, done.out);
  return file;
}

test('--measure writes the measurement with its sha, and --report-only never fails on a ceiling', () => {
  const build = _fixture({ sizes: { library: 25000 } });
  const file = _measure(build, _budgets(), SHA);
  const written = JSON.parse(readFileSync(file, 'utf8'));
  assert.equal(written.sha, SHA);
  assert.ok(written.totals.library.bytes > 20480, 'the fixture must be over its base to prove report-only');
  // The same build without --report-only is the nightly reading: over the ceiling fails.
  const nightly = _run(build, _budgets());
  assert.equal(nightly.code, 1, nightly.out);
  assert.match(nightly.out, /budget "library" exceeded/);
});

test('a PR is charged by its own delta: no growth passes even when main itself is over the ceiling', () => {
  const main = _fixture({ sizes: { library: 25000 } });
  const baseline = _measure(main, _budgets(), SHA);
  const pr = _fixture({ sizes: { library: 25000 } });
  const done = _runArgs(pr, _budgets(), ['--baseline', baseline, '--own', '9001,af--branch']);
  assert.equal(done.code, 0, done.out);
  assert.match(done.out, /library .* delta\s+0 .*\[ok\]/);
  assert.match(done.out, /::warning title=bundle budget ceiling::"library"/);
});

test('a PR that grows a surface fails on its own delta without a charge, and passes with its own charge', () => {
  const main = _fixture({ sizes: { library: 10000 } });
  const baseline = _measure(main, _budgets(), SHA);
  const pr = _fixture({ sizes: { library: 12500 } });
  const bare = _runArgs(pr, _budgets(), ['--baseline', baseline, '--own', '9001']);
  assert.equal(bare.code, 1, bare.out);
  assert.match(bare.out, /budget "library" grows by \d+ bytes gzip over main .* allowance of 0 \(plus 128 B build noise\) \(no charge file of its own\)/);

  const charged = _runArgs(
    pr,
    _budgets({ charges: { 'library/charges/9001.json': _charge({ measured_main: 10000, measured_with_pr: 12500 }) } }),
    ['--baseline', baseline, '--own', '9001'],
  );
  assert.equal(charged.code, 0, charged.out);
  assert.match(charged.out, /allowance\s+3000 \(9001\)\s+\[ok\]/);
});

test('a PR delta within the build-noise floor passes without a charge; past it, it fails', () => {
  const main = _fixture({ sizes: { library: 10000 } });
  const baseline = _measure(main, _budgets(), SHA);
  const noise = _runArgs(_fixture({ sizes: { library: 10040 } }), _budgets(), ['--baseline', baseline, '--own', '9001']);
  assert.equal(noise.code, 0, noise.out);
  assert.match(noise.out, /library .* delta\s+\d+ .*\[ok\]/);
  const real = _runArgs(_fixture({ sizes: { library: 10400 } }), _budgets(), ['--baseline', baseline, '--own', '9001']);
  assert.equal(real.code, 1, real.out);
  assert.match(real.out, /allowance of 0 \(plus 128 B build noise\) \(no charge file of its own\)/);
});

test("another PR's charge is not this PR's allowance", () => {
  const main = _fixture({ sizes: { library: 10000 } });
  const baseline = _measure(main, _budgets(), SHA);
  const pr = _fixture({ sizes: { library: 12500 } });
  const other = _runArgs(
    pr,
    _budgets({ charges: { 'library/charges/8002.json': _charge({ measured_main: 10000, measured_with_pr: 12500 }) } }),
    ['--baseline', baseline, '--own', '9001'],
  );
  assert.equal(other.code, 1, other.out);
  assert.match(other.out, /allowance of 0 \(plus 128 B build noise\) \(no charge file of its own\)/);
});

test('a surface the PR introduces has no baseline and is read against its own limit', () => {
  const main = _fixture();
  const baseline = _measure(main, _budgets(), SHA);
  const withBudgets = _budgets({
    extra: {},
  });
  // Remove the new surface from the baseline file to simulate main not knowing it yet.
  const parsed = JSON.parse(readFileSync(baseline, 'utf8'));
  delete parsed.totals['set-notes-editor'];
  writeFileSync(baseline, JSON.stringify(parsed));
  const ok = _runArgs(_fixture(), withBudgets, ['--baseline', baseline]);
  assert.equal(ok.code, 0, ok.out);
  assert.match(ok.out, /set-notes-editor: new surface, no baseline/);
  const over = _runArgs(_fixture({ sizes: { 'set-notes-editor': 25000 } }), withBudgets, ['--baseline', baseline]);
  assert.equal(over.code, 1, over.out);
  assert.match(over.out, /new budget "set-notes-editor" exceeded/);
});

test('an unreadable or shapeless baseline fails loudly instead of reading as zero growth', () => {
  const pr = _fixture();
  const missing = _runArgs(pr, _budgets(), ['--baseline', join(tmpdir(), 'no-such-measurement.json')]);
  assert.equal(missing.code, 1, missing.out);
  assert.match(missing.out, /cannot read as JSON/);
  const shapeless = join(mkdtempSync(join(tmpdir(), 'bundle-measure-')), 'bad.json');
  writeFileSync(shapeless, JSON.stringify({ sha: SHA }));
  const bad = _runArgs(pr, _budgets(), ['--baseline', shapeless]);
  assert.equal(bad.code, 1, bad.out);
  assert.match(bad.out, /no "totals" object/);
  const conflicting = _runArgs(pr, _budgets(), ['--baseline', shapeless, '--report-only']);
  assert.equal(conflicting.code, 1, conflicting.out);
  assert.match(conflicting.out, /different readings/);
});

test('coverage still fails in delta mode: an unreached chunk is never excused by a zero delta', () => {
  const main = _fixture();
  const baseline = _measure(main, _budgets(), SHA);
  const pr = _fixture({ extras: { files: { 'chunks/orphan.js': 'export const z=1;' } } });
  const done = _runArgs(pr, _budgets(), ['--baseline', baseline]);
  assert.equal(done.code, 1, done.out);
  assert.match(done.out, /belong to no budget/);
});

test('ARTTHUMB-8728d the /playlists card grid is charged to its own playlists-grid bucket, not other-lazy', () => {
  const { code, out } = _run(_fixture({ sizes: { 'playlists-grid': _limitOf('playlists-grid') + 8000 } }));
  assert.equal(code, 1, `expected a non-zero exit\n${out}`);
  assert.match(out, /budget "playlists-grid" exceeded/);
  assert.doesNotMatch(out, /budget "other-lazy" exceeded/);
});

test('SKIN-8871B the /practice replica is charged to its own practice-replica bucket, not other-lazy', () => {
  const { code, out } = _run(_fixture({ sizes: { 'practice-replica': _limitOf('practice-replica') + 8000 } }));
  assert.equal(code, 1, `expected a non-zero exit\n${out}`);
  assert.match(out, /budget "practice-replica" exceeded/);
  assert.doesNotMatch(out, /budget "other-lazy" exceeded/);
});
