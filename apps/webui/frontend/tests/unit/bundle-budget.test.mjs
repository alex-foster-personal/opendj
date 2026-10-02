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
// - if the library limit stops being exactly 262144 then the figure moved
//   without a reviewed note: 256000 was the inherited figure this repair
//   explicitly did not raise; +1 KiB was added Mon 21 Sep 2026 (PR #3737) for
//   the live-transport probe on the client error path, with main already
//   within 80 bytes of the limit, and +1 KiB Wed 23 Sep 2026 (PR #3681) for
//   the multiple-folder rows in the statically imported SetupOverlay, with
//   main at 277 bytes of headroom, and +1 KiB Thu 24 Sep 2026 (PR #3865) for
//   the deck's stem hydrating state, with main already 319 bytes over, and
//   +1 KiB Fri 2 Oct 2026 (PR #4897) for the first-run wizard's plain-language
//   copy, with main + that PR 142 bytes over, and +2 KiB Fri 2 Oct 2026 (PR #4908)
//   for the native headphone cue sink plus #4906's stems on a playing deck, with
//   main at 153 bytes of headroom. Any further move is a deliberate act with
//   its own dated note in scripts/bundle-budget.mjs and a new pin here.
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

  write('nodes/0.CCCCCCCC.js', 'import"../chunks/shared.js";'); // root layout
  write('nodes/1.CCCCCCCC.js', 'import"../chunks/shared.js";'); // error node
  write('nodes/2.CCCCCCCC.js', 'import"../chunks/lib.js";'); // route "/"
  write('nodes/3.CCCCCCCC.js', 'import"../chunks/other.js";'); // route "/sets"
  write(`nodes/6.CCCCCCCC.js`, `import"../chunks/perf.js";${extras.perfBody ?? ''}`); // "/performance"

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

function _run(root) {
  try {
    const stdout = execFileSync('node', [GATE, '--root', root], { encoding: 'utf8', stdio: 'pipe' });
    return { code: 0, out: stdout };
  } catch (error) {
    return { code: error.status, out: `${error.stdout ?? ''}${error.stderr ?? ''}` };
  }
}

// ------------------------------------------------------------------ tests ---

test('the library limit is exactly 262144: the inherited 256000 plus six reviewed KiB', () => {
  const source = execFileSync('node', ['-e', `process.stdout.write(require("fs").readFileSync(${JSON.stringify(GATE)},"utf8"))`], {
    encoding: 'utf8',
  });
  assert.match(
    source,
    /\{ name: 'library', limit: 262144,/,
    'the library figure must not move without a dated note in the gate and a new pin here'
  );
  assert.match(source, /RAISED Mon 21 Sep 2026 \(\+1 KiB, PR #3737/, 'the raise must carry its note');
  assert.match(source, /RAISED Wed 23 Sep 2026 \(\+1 KiB, PR #3681/, 'the raise must carry its note');
  assert.match(source, /RAISED Thu 24 Sep 2026 \(\+1 KiB, PR #3865/, 'the raise must carry its note');
  assert.match(source, /RAISED Sat 26 Sep 2026 \(\+2 KiB, PR #3837/, 'the raise must carry its note');
  assert.match(source, /RAISED Fri 2 Oct 2026 \(\+1 KiB, PR #4897/, 'the raise must carry its note');
  assert.match(source, /RAISED Fri 2 Oct 2026 \(\+2 KiB, PR #4908/, 'the raise must carry its note');
});

test('a clean build passes and every emitted chunk is charged to a budget', () => {
  const { code, out } = _run(_fixture());
  assert.equal(code, 0, out);
  assert.match(out, /all bundle budgets satisfied/);
  assert.match(out, /coverage: 12 of 12 emitted JS files charged to a budget/);
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
 * Read a budget's limit out of the gate rather than restating it here.
 *
 * The `other-lazy` overflow below used to be the literal 70000, chosen to clear
 * a 67584 limit. When that limit moved to 110592 for the Q18 FLAC decoder
 * (PR #1691), 70000 stopped overflowing anything and this guard went GREEN
 * while asserting that a budget fails - the exact "decorative budget" failure
 * the header above says these tests exist to prevent, one level up. A guard
 * that stops guarding when the thing it guards changes is worse than no guard,
 * so the number is derived now and cannot go stale again.
 */
function _limitOf(name) {
  const source = readFileSync(GATE, 'utf8');
  const match = source.match(new RegExp(`name: '${name}', limit: (\\d+)`));
  assert.ok(match, `could not read the ${name} limit out of ${GATE}`);
  return Number(match[1]);
}

for (const surface of ['library', 'performance', 'other-lazy']) {
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
  assert.match(clean.out, /coverage: 13 of 13 emitted JS files charged to a budget/);

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
