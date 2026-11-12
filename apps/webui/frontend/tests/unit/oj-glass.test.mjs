import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, before, mock, test } from 'node:test';

import { compile } from 'svelte/compiler';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

let gestures;
let policy;
let ssr;

before(async () => {
	gestures = await loadTypeScriptModule('src/lib/components/assistant/oj-glass-gestures.ts');
	policy = await loadTypeScriptModule('src/lib/components/assistant/oj-glass-motion.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as OjGlass } from '$lib/components/assistant/OjGlass.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

afterEach(() => {
	mock.timers.reset();
});

function contrastRatio(fgHex, bgHex) {
	const parse = (hex) => {
		const n = hex.replace('#', '');
		const r = parseInt(n.slice(0, 2), 16) / 255;
		const g = parseInt(n.slice(2, 4), 16) / 255;
		const b = parseInt(n.slice(4, 6), 16) / 255;
		const lin = (c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
		return [lin(r), lin(g), lin(b)];
	};
	const lum = (rgb) => 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
	const L1 = lum(parse(fgHex));
	const L2 = lum(parse(bgHex));
	const lighter = Math.max(L1, L2);
	const darker = Math.min(L1, L2);
	return (lighter + 0.05) / (darker + 0.05);
}

test('nextGestureDelayMs stays in [20000, 40000]', () => {
	for (const r of [0, 0.5, 0.999999, 1]) {
		const ms = gestures.nextGestureDelayMs(() => r);
		assert.ok(ms >= policy.OJ_GESTURE_MIN_MS && ms <= policy.OJ_GESTURE_MAX_MS);
	}
});

test('pickGesture returns smile and slosh', () => {
	assert.equal(
		gestures.pickGesture(() => 0),
		'smile'
	);
	assert.equal(
		gestures.pickGesture(() => 0.99),
		'slosh'
	);
});

test('think suppresses gestures for 60s of fake time', () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	const full = policy.OJ_MOTION_FULL;
	let currentGesture = null;
	const calls = [];
	const scheduler = gestures.createGestureScheduler({
		getState: () => 'think',
		getPolicy: () => full,
		onGesture: (g) => {
			currentGesture = g;
			if (g !== null) calls.push(g);
		},
		random: () => 0
	});
	scheduler.start();
	for (let t = 0; t <= 60_000; t += 1_000) {
		mock.timers.tick(1_000);
		const classes = gestures.ojGlassClasses('think', currentGesture, full);
		assert.ok(classes.includes('think'));
		assert.ok(!classes.some((c) => /smile|slosh/.test(c)));
	}
	assert.equal(calls.length, 0);
	scheduler.stop();
	const html = ssr.render(ssr.OjGlass, { props: { state: 'think' } }).body;
	assert.match(html, /\bthink\b/);
	assert.doesNotMatch(html, /\bsmile\b/);
	assert.doesNotMatch(html, /\bslosh\b/);
});

test('idleGestures false never applies gesture classes', () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	const pol = { idleGestures: false, bubbles: true, fillMode: 'animated' };
	const calls = [];
	const scheduler = gestures.createGestureScheduler({
		getState: () => 'idle',
		getPolicy: () => pol,
		onGesture: (g) => {
			if (g !== null) calls.push(g);
		},
		random: () => 0
	});
	scheduler.start();
	mock.timers.tick(120_000);
	assert.equal(calls.length, 0);
	assert.ok(!gestures.ojGlassClasses('idle', 'smile', pol).some((c) => c.startsWith('gesture-')));
	assert.ok(!gestures.ojGlassClasses('idle', 'slosh', pol).some((c) => c.startsWith('gesture-')));
	scheduler.stop();
	const html = ssr.render(ssr.OjGlass, { props: { state: 'idle', policy: pol } }).body;
	assert.doesNotMatch(html, /gesture-/);
});

test('idle cadence: first gesture at 20s, clear at 21.2s, spacing >= 20s', () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	const full = policy.OJ_MOTION_FULL;
	let elapsed = 0;
	const fired = [];
	const scheduler = gestures.createGestureScheduler({
		getState: () => 'idle',
		getPolicy: () => full,
		onGesture: (g) => {
			if (g !== null) fired.push(elapsed);
		},
		random: () => 0
	});
	scheduler.start();
	elapsed += 19_999;
	mock.timers.tick(19_999);
	assert.equal(fired.length, 0);
	elapsed += 1;
	mock.timers.tick(1);
	assert.equal(fired.length, 1);
	assert.equal(fired[0], 20_000);
	elapsed += 1_200;
	mock.timers.tick(1_200);
	elapsed += 18_800;
	mock.timers.tick(18_800);
	assert.ok(fired.length >= 1);
	let minGap = Infinity;
	for (let i = 1; i < fired.length; i++) {
		minGap = Math.min(minGap, fired[i] - fired[i - 1]);
	}
	if (fired.length > 1) {
		assert.ok(minGap >= 20_000);
	}
	scheduler.stop();
	const countBefore = fired.length;
	mock.timers.tick(60_000);
	assert.equal(fired.length, countBefore);
});

test('high random delays first gesture until near 40s', () => {
	mock.timers.enable({ apis: ['setTimeout'] });
	const full = policy.OJ_MOTION_FULL;
	let sawGesture = false;
	const scheduler = gestures.createGestureScheduler({
		getState: () => 'idle',
		getPolicy: () => full,
		onGesture: (g) => {
			if (g !== null) sawGesture = true;
		},
		random: () => 0.999999
	});
	scheduler.start();
	mock.timers.tick(39_979);
	assert.equal(sawGesture, false);
	scheduler.stop();
});

test('bubbles false renders no bubble elements', () => {
	let html = ssr.render(ssr.OjGlass, { props: { state: 'idle', bubbles: false } }).body;
	assert.doesNotMatch(html, /data-oj-glass-bubble/);
	html = ssr.render(ssr.OjGlass, { props: { state: 'idle', bubbles: true } }).body;
	const count = (html.match(/data-oj-glass-bubble/g) ?? []).length;
	assert.equal(count, 3);
	html = ssr.render(ssr.OjGlass, {
		props: {
			state: 'idle',
			bubbles: true,
			policy: { idleGestures: false, bubbles: false, fillMode: 'static' }
		}
	}).body;
	assert.doesNotMatch(html, /data-oj-glass-bubble/);
});

test('a11y: role img and name OJ', () => {
	const html = ssr.render(ssr.OjGlass, { props: {} }).body;
	assert.match(html, /role="img"/);
	assert.match(html, /aria-label="OJ"/);
	assert.match(html, /<title[^>]*>OJ<\/title>/);
});

test('OjGlassGraphic compiles with scoped motion CSS and no unused selectors', () => {
	const graphicPath = `${FRONTEND_ROOT}/src/lib/components/assistant/OjGlassGraphic.svelte`;
	const source = readFileSync(graphicPath, 'utf8');
	const result = compile(source, { filename: 'OjGlassGraphic.svelte', generate: 'server' });
	const unused = (result.warnings ?? []).filter((w) => w.code === 'css_unused_selector');
	assert.deepEqual(unused, [], unused.map((w) => w.message).join('\n'));
	const css = result.css?.code ?? '';
	assert.match(css, /\.mouth/);
	assert.match(css, /\.eyes/);
	assert.match(css, /\.bubble/);
	assert.match(css, /@keyframes\s+\S*oj-blink/);
});

test('contrast: eyes and outline use currentColor on var(--fg) against each theme --bg', () => {
	const graphic = readFileSync(
		`${FRONTEND_ROOT}/src/lib/components/assistant/OjGlassGraphic.svelte`,
		'utf8'
	);
	assert.match(graphic, /color:\s*var\(--fg\)/);
	assert.match(graphic, /stroke="currentColor"/);
	assert.match(graphic, /fill="currentColor"/);
	const appCss = readFileSync(`${FRONTEND_ROOT}/src/app.css`, 'utf8');
	const fgMatches = [...appCss.matchAll(/--fg:\s*(#[0-9a-fA-F]+)/g)].map((m) => m[1]);
	const bgMatches = [...appCss.matchAll(/--bg:\s*(#[0-9a-fA-F]+)/g)].map((m) => m[1]);
	assert.equal(fgMatches.length, bgMatches.length);
	for (let i = 0; i < fgMatches.length; i++) {
		assert.ok(contrastRatio(fgMatches[i], bgMatches[i]) >= 3);
	}
});
