/**
 * @pytest.mark.requirement LIBM-174
 * The library track list scrolls natively (supersedes PVPIN-19, the maintainer, Tue 6 Oct
 * 2026: "Anyone can adjust their mouse sensitivity. The lag is VERY not worth it.").
 *
 * [if] the track list is scrolled with a wheel or trackpad [then] no app wheel
 *   handler runs and the browser scrolls it [else stop].
 *
 * Regression lines:
 * - if TrackTable's scroll container gets a wheel action or listener again then
 *   wheel scrolling leaves the compositor and stalls whenever the app is busy -> broken
 * - if any module under src/lib binds a non-passive wheel listener on window or
 *   document outside a selection's lifetime then EVERY wheel event on the page,
 *   the library list included, waits on the main thread (PERF-UI-13, #10227)
 */
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const path = (p) => fileURLToPath(new URL(`../../${p}`, import.meta.url));
const read = (p) => readFileSync(path(p), 'utf8');

test('the track list scroll container has no wheel handler', () => {
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	const wrapAt = table.indexOf('class="table-wrap"');
	assert.ok(wrapAt > 0, 'table-wrap missing (control: the probe found the container)');
	const tagEnd = table.indexOf('\t>\n', wrapAt);
	assert.ok(tagEnd > wrapAt, 'end of the table-wrap tag not found (control)');
	const tag = table.slice(wrapAt, tagEnd);
	assert.match(tag, /onscroll=/, 'control: the slice is the real tag, which carries onscroll');
	assert.doesNotMatch(tag, /wheel/i, 'no use: action or handler named for the wheel');
	assert.doesNotMatch(
		table,
		/addEventListener\(\s*['"]wheel['"]/,
		'no wheel listener anywhere in TrackTable'
	);
});

/** Every .ts, .svelte.ts and .svelte file under a directory, recursively. */
function sourceFiles(dir) {
	const out = [];
	for (const name of readdirSync(dir)) {
		const full = join(dir, name);
		if (statSync(full).isDirectory()) out.push(...sourceFiles(full));
		else if (/\.(ts|svelte)$/.test(name)) out.push(full);
	}
	return out;
}

/** Name of the innermost `function <name>(` declared before `index`, or null. */
function enclosingFunctionName(source, index) {
	const head = source.slice(0, index);
	let name = null;
	for (const m of head.matchAll(/function\s+([A-Za-z_$][\w$]*)\s*\(/g)) name = m[1];
	return name;
}

/**
 * Page-lifetime non-passive wheel bindings that predate PERF-UI-13 and are
 * out of #10227's scope, each with the issue that moves it to a selection
 * lifetime. Adding a line here is a product decision, not a fix.
 */
const KNOWN_PAGE_LIFETIME_WHEEL = new Map([
	[
		'src/lib/rb/mixer-performance-listeners.ts',
		'MIXUX-08 horizontal wheel on /performance, follow-up #10369'
	]
]);

test('PERF-UI-13: no non-passive window or document wheel listener outside a selection lifetime', () => {
	const files = sourceFiles(path('src/lib'));
	assert.ok(files.length > 50, `control: the walk found only ${files.length} files`);
	const nonPassive =
		/\b(window|document)\.addEventListener\(\s*['"]wheel['"][\s\S]{0,200}?passive:\s*false/g;
	const offenders = [];
	let matches = 0;
	const seenKnown = new Set();
	for (const file of files) {
		const source = readFileSync(file, 'utf8');
		if (!source.includes('wheel')) continue;
		// POSIX separators, so the allowlist matches on Windows too.
		const rel = file.slice(path('').length).split(sep).join('/');
		for (const m of source.matchAll(nonPassive)) {
			matches += 1;
			if (KNOWN_PAGE_LIFETIME_WHEEL.has(rel)) {
				seenKnown.add(rel);
				continue;
			}
			const fn = enclosingFunctionName(source, m.index);
			if (fn === null || !/Selection|select/.test(fn))
				offenders.push(`${rel} in ${fn ?? '<module scope>'}`);
		}
	}
	// A known entry that no longer binds has been fixed: drop it from the map
	// so the guard tightens instead of carrying a dead exemption.
	assert.deepEqual(
		[...KNOWN_PAGE_LIFETIME_WHEEL.keys()].filter((k) => !seenKnown.has(k)),
		[],
		'a KNOWN_PAGE_LIFETIME_WHEEL entry no longer matches; remove it'
	);
	// Control: the knob selection's own wheel is the one legitimate binding, so
	// the regex must find at least that or it is not seeing anything.
	assert.ok(
		matches >= 2,
		`control: the probe found ${matches} non-passive window wheel bindings, expected the selection one plus the known entries`
	);
	assert.deepEqual(
		offenders,
		[],
		'a non-passive wheel listener is bound for the page lifetime, not a selection'
	);
});

test('the half-speed wheel helper is gone, not just unused', () => {
	assert.equal(existsSync(path('src/lib/rb/library-wheel-scroll.ts')), false);
	assert.doesNotMatch(
		read('src/lib/components/rb/browser/track-table-support.ts'),
		/library-wheel-scroll/
	);
	// Negative control: a path that does exist reports present, so the probe can say yes.
	assert.equal(existsSync(path('src/lib/rb/playing-gate.ts')), true);
});
