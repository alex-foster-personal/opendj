import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, describe, it, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * A11Y-11811: one reduced-motion policy (OS media query OR Appearance pref).
 *
 * [if] the pref is on but a non-essential fade still animates [then] the policy is broken.
 * [if] a progress bar stops filling under reduce motion [then] .motion-essential failed.
 * [if] a new animation lands with no reduce rule [then] the source scan must fail.
 */

const FRONTEND = fileURLToPath(new URL('../../', import.meta.url));
const SRC = join(FRONTEND, 'src');
/** SvelteKit's runtime has no build in this harness; the SSR load below stubs it. */
const FAKE_NAVIGATION = new URL('./fake-app-navigation.mjs', import.meta.url).pathname;
const FAKE_STORES = new URL('./fake-app-stores.mjs', import.meta.url).pathname;

function read(rel) {
	return readFileSync(join(SRC, rel), 'utf8');
}

function walkFiles(root, exts, acc = []) {
	for (const name of readdirSync(root)) {
		const p = join(root, name);
		const st = statSync(p);
		if (st.isDirectory()) walkFiles(p, exts, acc);
		else if (exts.some((e) => name.endsWith(e))) acc.push(p);
	}
	return acc;
}

function stripComments(text) {
	return text
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/\/\/[^\n]*/g, '')
		.replace(/<!--[\s\S]*?-->/g, '');
}

const animRe = /@keyframes\b|(?<![-\w])transition\s*:(?!\s*none\b)/;
const passRe = /prefers-reduced-motion|\[data-reduce-motion\]|\bmotion-essential\b/;
const matchMediaRe =
	/matchMedia\s*\??\.?\s*\(\s*(['"`]\s*\(\s*prefers-reduced-motion|REDUCED_MOTION_QUERY)/;

let motion;
let reduced;

before(async () => {
	reduced = await loadTypeScriptModule('src/lib/motion/reduced-motion.ts');
	motion = await loadTypeScriptModule('src/lib/motion/collapse.ts');
	await loadSvelteSsrModule(
		[
			"export { default as SettingsOverlay } from '$lib/components/settings/SettingsOverlay.svelte';",
			"export { default as StemsProgress } from '$lib/components/rb/StemsProgress.svelte';",
			"export { default as Layout } from './src/routes/+layout.svelte';"
		].join('\n'),
		{ alias: { '$app/navigation': FAKE_NAVIGATION, '$app/stores': FAKE_STORES } }
	);
});

describe('prefersReducedMotion truth table', () => {
	it('ORs media and pref', () => {
		assert.equal(reduced.prefersReducedMotion({ media: false, pref: true }), true);
		assert.equal(reduced.prefersReducedMotion({ media: true, pref: false }), true);
		assert.equal(reduced.prefersReducedMotion({ media: true, pref: true }), true);
		assert.equal(reduced.prefersReducedMotion({ media: false, pref: false }), false);
	});

	it('throws on non-boolean input members', () => {
		assert.throws(() => reduced.prefersReducedMotion({ media: 'x', pref: false }), TypeError);
	});
});

describe('live read and DOM attribute', () => {
	it('reads pref and media through the helper', () => {
		const original = globalThis.window;
		let media = false;
		globalThis.window = {
			matchMedia: (q) => ({
				matches: q === reduced.REDUCED_MOTION_QUERY && media,
				addEventListener() {},
				removeEventListener() {}
			})
		};
		try {
			reduced.setReduceMotionPref(false);
			assert.equal(reduced.prefersReducedMotion(), false);
			reduced.setReduceMotionPref(true);
			assert.equal(reduced.prefersReducedMotion(), true);
			reduced.setReduceMotionPref(false);
			media = true;
			assert.equal(reduced.prefersReducedMotion(), true);
			assert.throws(() => reduced.setReduceMotionPref('yes'), TypeError);
		} finally {
			globalThis.window = original;
		}
	});

	it('toggles data-reduce-motion on the root', () => {
		const calls = [];
		const fake = {
			toggleAttribute(name, on) {
				calls.push([name, on]);
			}
		};
		reduced.setReduceMotionPref(true);
		assert.equal(reduced.applyReducedMotionAttr(fake), true);
		assert.deepEqual(calls.at(-1), ['data-reduce-motion', true]);
		reduced.setReduceMotionPref(false);
		globalThis.window = {
			matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
		};
		assert.equal(reduced.applyReducedMotionAttr(fake), false);
	});

	it('watchReducedMotionMedia fires on change', () => {
		const handlers = new Map();
		globalThis.window = {
			matchMedia: () => ({
				matches: false,
				addEventListener(type, fn) {
					handlers.set(type, fn);
				},
				removeEventListener(type, fn) {
					if (handlers.get(type) === fn) handlers.delete(type);
				}
			})
		};
		let n = 0;
		const off = reduced.watchReducedMotionMedia(() => {
			n += 1;
		});
		handlers.get('change')?.();
		assert.equal(n, 1);
		off();
		handlers.get('change')?.();
		assert.equal(n, 1);
	});
});

describe('collapse snaps with reduced motion pref', () => {
	it('collapseDuration and collapse honor reduced motion', () => {
		assert.equal(motion.collapseDuration(3, true), 0);
		const node = { children: [{ childElementCount: 3 }] };
		const original = globalThis.window;
		globalThis.window = {
			matchMedia: (q) => ({
				matches: q === reduced.REDUCED_MOTION_QUERY,
				addEventListener() {},
				removeEventListener() {}
			})
		};
		try {
			assert.equal(motion.collapse(node).duration, 0);
			globalThis.window = {
				matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
			};
			assert.equal(motion.collapse(node).duration, 100);
		} finally {
			globalThis.window = original;
		}
	});
});

describe('single matchMedia caller under src/lib', () => {
	it('only reduced-motion.ts may call matchMedia for the query', () => {
		const libRoot = join(SRC, 'lib');
		const offenders = [];
		for (const file of walkFiles(libRoot, ['.ts', '.svelte', '.js', '.mjs'])) {
			const rel = file.slice(SRC.length + 1);
			if (rel === 'lib/motion/reduced-motion.ts') continue;
			const body = stripComments(readFileSync(file, 'utf8'));
			if (matchMediaRe.test(body)) offenders.push(rel);
		}
		assert.deepEqual(offenders, []);
	});

	it('migrated modules import the helper', () => {
		const importRe = /(?:\$lib\/motion\/reduced-motion|\.\/reduced-motion)/;
		for (const rel of [
			'lib/motion/collapse.ts',
			'lib/library/section-motion.ts',
			'lib/components/rb/wave/WaveRow.svelte',
			'lib/components/rb/PairEdge.svelte',
			'lib/components/rb/browser/AutoPlayWalkthrough.svelte',
			'lib/components/rb/browser/SpinnerIcon.contract.ts'
		]) {
			assert.match(read(rel), importRe);
		}
	});
});

describe('component animation source scan', () => {
	function scanComponents() {
		const root = join(SRC, 'lib/components');
		const failures = [];
		const animated = [];
		for (const file of walkFiles(root, ['.svelte', '.css'])) {
			const raw = readFileSync(file, 'utf8');
			const body = stripComments(raw);
			if (!animRe.test(body)) continue;
			animated.push(file.slice(SRC.length + 1));
			if (!passRe.test(body)) failures.push(file.slice(SRC.length + 1));
		}
		return { failures, animated };
	}

	it('every animating component file carries a reduce rule or motion-essential', () => {
		const { failures, animated } = scanComponents();
		assert.ok(animated.length >= 50, `expected >= 50 animated files, got ${animated.length}`);
		assert.ok(
			animated.includes('lib/components/settings/SettingsOverlay.svelte'),
			'SettingsOverlay must be in the scan'
		);
		assert.ok(
			animated.includes('lib/components/rb/PositionBar.svelte'),
			'PositionBar must be in the scan'
		);
		assert.deepEqual(failures, []);
	});

	it('predicate negative control', () => {
		const fail = '<style>.a{transition: opacity 1s}</style>';
		const failBody = stripComments(fail);
		assert.ok(animRe.test(failBody));
		assert.ok(!passRe.test(failBody));
		const commentOnly = `${fail}/* respects prefers-reduced-motion */`;
		const commentOnlyBody = stripComments(commentOnly);
		assert.ok(animRe.test(commentOnlyBody));
		assert.ok(
			!passRe.test(commentOnlyBody),
			'comment-only reduce mention must not satisfy the scan'
		);
		const pass = `${fail}<style>:global(html[data-reduce-motion]) .a{}</style>`;
		assert.ok(passRe.test(stripComments(pass)));
	});
});

test('app.css carries mirrored reduced-motion rules', () => {
	const css = readFileSync(join(SRC, 'app.css'), 'utf8');
	assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
	assert.match(css, /html\[data-reduce-motion\]/);
	assert.match(css, /:not\(\.motion-essential\)/);
	assert.match(css, /animation-duration: 0\.01ms !important/);
	assert.match(css, /transition-duration: 0\.01ms !important/);
	assert.match(css, /animation-iteration-count: 1 !important/);
});

test('overlay and stems acceptance fixtures', () => {
	assert.match(read('lib/components/settings/SettingsOverlay.svelte'), /\[data-reduce-motion\]/);
	const hotkeysRoot = join(SRC, 'lib/components/rb/hotkeys');
	for (const file of walkFiles(hotkeysRoot, ['.svelte', '.css'])) {
		const body = stripComments(readFileSync(file, 'utf8'));
		if (animRe.test(body)) {
			assert.fail('a hotkeys overlay fade needs a reduce rule');
		}
	}
	assert.match(
		read('lib/components/rb/StemsProgress.svelte'),
		/class="stems-fill motion-essential"/
	);
});

test('root layout wires reduced motion', () => {
	const layout = readFileSync(join(SRC, 'routes/+layout.svelte'), 'utf8');
	assert.match(layout, /applyReducedMotionAttr/);
	assert.match(layout, /setReduceMotionPref\(uiPrefs\.reduce_motion\)/);
	assert.match(layout, /watchReducedMotionMedia/);
	assert.match(layout, /subscribeKind\('ui_prefs'/);
	assert.match(layout, /refreshReduceMotionFromDisk/);
});

describe('makeReduceMotionRefresher', () => {
	test('agent PUT reaches the open page', async () => {
		const hydrate = await loadTypeScriptModule('src/lib/rb/prefs-hydrate.ts', {
			viteApiBase: 'https://reduced-motion.example.test'
		});
		const original = globalThis.fetch;
		let reads = 0;
		let disk = true;
		globalThis.fetch = async () => {
			reads += 1;
			return new Response(JSON.stringify({ reduce_motion: disk }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		};
		try {
			const prefs = { reduce_motion: false };
			let persisted = 0;
			const refresh = hydrate.makeReduceMotionRefresher(
				prefs,
				() => {
					persisted += 1;
				},
				(message) => assert.fail(message),
				0
			);
			await Promise.all([refresh(), refresh()]);
			assert.equal(prefs.reduce_motion, true);
			assert.equal(persisted, 1);
			globalThis.fetch = async () =>
				new Response(JSON.stringify({ reduce_motion: 'bad' }), {
					status: 200,
					headers: { 'content-type': 'application/json' }
				});
			const fail = hydrate.makeReduceMotionRefresher(
				prefs,
				() => {},
				() => {},
				0
			);
			assert.equal(await fail(), false);
		} finally {
			globalThis.fetch = original;
		}
	});
});

test('filterSettings finds reduce_motion', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/search.ts');
	for (const q of ['motion', 'animation', 'reduce']) {
		const hits = mod.filterSettings(q, { hideTodo: true, group: null });
		const def = hits.keyword.find((s) => s.id === 'reduce_motion');
		assert.ok(def, `reduce_motion must be searchable for ${q}`);
		assert.equal(def.group, 'appearance');
	}
});
