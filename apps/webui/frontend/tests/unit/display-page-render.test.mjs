/**
 * Now-playing display page render (issue #10450).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mock, test } from 'node:test';
import {
	DRIFT_CYCLE,
	chips,
	driftOffset,
	formatClock,
	isReconnecting,
	nextLine,
	normalizeState,
	progressFraction,
	renderDisplay,
	resolveVariant,
	safeArtwork
} from '../../static/now-playing/display-render.js';

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, '../../static/now-playing');

function playingState(overrides = {}) {
	const { playing: playingOver, next, variant, title_hidden, ...rest } = overrides;
	return normalizeState({
		playing: {
			deck: 1,
			title: 'Test Title',
			artist: 'Test Artist',
			key_camelot: '8A',
			bpm: 128,
			elapsed_s: 60,
			duration_s: 300,
			artwork: '/now-playing/api/artwork',
			...(playingOver ?? {})
		},
		next: next ?? { title: 'Next Song', artist: 'Next Artist' },
		variant: variant ?? 'poster',
		title_hidden: title_hidden ?? false,
		...rest
	});
}

function parseDriftTransform(html) {
	const match = html.match(
		/data-np-drift[^>]*style="transform: translate\((-?\d+)px, (-?\d+)px\)"/
	);
	assert.ok(match, 'drift transform');
	return { x: Number(match[1]), y: Number(match[2]) };
}

test('variant resolution and poster vs strip markup', () => {
	assert.equal(resolveVariant('strip', ''), 'strip');
	let html = renderDisplay(playingState({ variant: 'strip' }), {
		variant: 'strip',
		nowMs: 0,
		reconnecting: false
	});
	assert.match(html, /data-variant="strip"/);
	assert.match(html, /np--strip/);
	assert.doesNotMatch(html, /class="np__bg"/);

	assert.equal(resolveVariant('strip', '?variant=poster'), 'poster');
	html = renderDisplay(playingState(), {
		variant: 'poster',
		nowMs: 0,
		reconnecting: false
	});
	assert.match(html, /data-variant="poster"/);
	assert.match(html, /np__bg/);

	assert.equal(resolveVariant(undefined, ''), 'poster');
	assert.equal(resolveVariant('bogus', '?variant=nope'), 'poster');
	assert.equal(resolveVariant('poster', '?variant=strip'), 'strip');
});

test('title_hidden shows ID and hides real titles', () => {
	const state = playingState({
		title_hidden: true,
		playing: {
			title: 'Secret Track',
			artist: 'Hidden Artist',
			artwork: '/now-playing/api/artwork'
		}
	});
	const html = renderDisplay(state, { variant: 'poster', nowMs: 0, reconnecting: false });
	const titleMatch = html.match(/data-np-title[^>]*>([^<]*)</);
	assert.equal(titleMatch[1], 'ID');
	assert.doesNotMatch(html, /Secret Track/);
	assert.doesNotMatch(html, /Hidden Artist/);
	assert.match(html, /data-np-art[^>]*src="\/now-playing\/api\/artwork"/);
});

test('idle screen with clock for both variants', () => {
	const fixed = Date.UTC(2026, 10, 10, 21, 4, 0);
	for (const variant of ['poster', 'strip']) {
		const html = renderDisplay(normalizeState({ playing: null, variant }), {
			variant,
			nowMs: fixed,
			reconnecting: false
		});
		assert.match(html, /data-np-idle/);
		assert.match(html, />Open DJ</);
		assert.match(html, />Up next soon</);
		assert.doesNotMatch(html, /data-np-card/);
		const clock = formatClock(new Date(fixed));
		assert.match(html, new RegExp(`data-np-clock[^>]*>${clock}`));
	}
});

test('drift moves within 8px every minute', () => {
	const T0 = Date.UTC(2026, 0, 1, 12, 0, 0);
	mock.timers.enable({ apis: ['Date'], now: T0 });
	try {
		const html1 = renderDisplay(normalizeState(null), {
			variant: 'poster',
			nowMs: Date.now(),
			reconnecting: false
		});
		const a = parseDriftTransform(html1);
		assert.ok(Math.hypot(a.x, a.y) <= 8);
		mock.timers.tick(60000);
		const html2 = renderDisplay(normalizeState(null), {
			variant: 'poster',
			nowMs: Date.now(),
			reconnecting: false
		});
		const b = parseDriftTransform(html2);
		assert.ok(Math.hypot(b.x, b.y) <= 8);
		assert.notDeepEqual(a, b);

		let prev = driftOffset(T0);
		for (let m = 1; m <= 1440; m += 1) {
			const t = T0 + m * 60000;
			const cur = driftOffset(t);
			assert.ok(Math.hypot(cur.x, cur.y) <= 8);
			assert.notDeepEqual(cur, prev);
			prev = cur;
		}
	} finally {
		mock.timers.reset();
	}
});

test('reconnecting state', () => {
	const t = 1000;
	assert.equal(isReconnecting(t + 5000, t), false);
	assert.equal(isReconnecting(t + 5001, t), true);
	const on = renderDisplay(normalizeState(null), {
		variant: 'poster',
		nowMs: t,
		reconnecting: true
	});
	assert.match(on, /np--reconnecting/);
	assert.match(on, /data-np-reconnecting(?![^>]*hidden)/);
	const off = renderDisplay(normalizeState(null), {
		variant: 'poster',
		nowMs: t,
		reconnecting: false
	});
	assert.match(off, /data-np-reconnecting hidden/);
});

test('escaping and safe artwork', () => {
	const state = playingState({
		playing: {
			title: '<img src=x onerror=alert(1)>',
			artist: 'A',
			artwork: '/ok.png'
		}
	});
	const html = renderDisplay(state, { variant: 'poster', nowMs: 0, reconnecting: false });
	assert.doesNotMatch(html, /<img src=x/);
	assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);

	assert.equal(safeArtwork('https://example.invalid/a.png'), null);
	assert.equal(safeArtwork('//example.invalid/a.png'), null);
	const bad = playingState({
		playing: { title: 'T', artist: 'A', artwork: 'https://example.invalid/a.png' }
	});
	const badHtml = renderDisplay(bad, { variant: 'poster', nowMs: 0, reconnecting: false });
	assert.doesNotMatch(badHtml, /src="https:\/\/example/);
	assert.match(badHtml, /np__cover-ph/);
});

test('progressFraction formatClock nextLine chips', () => {
	assert.equal(progressFraction(null), 0);
	assert.equal(progressFraction({ elapsed_s: 150, duration_s: 300 }), 0.5);
	assert.equal(progressFraction({ elapsed_s: 400, duration_s: 300 }), 1);
	assert.equal(progressFraction({ elapsed_s: -1, duration_s: 300 }), 0);
	assert.equal(formatClock(new Date(2026, 10, 10, 9, 5)), '09:05');
	assert.equal(nextLine({ title: 'T', artist: 'A' }), 'Next: T, A');
	assert.equal(nextLine({ title: 'T' }), 'Next: T');
	assert.equal(nextLine(null), '');
	const c = chips({ bpm: 127.6, key_musical: 'Am' });
	assert.equal(c.bpm, '128 BPM');
	assert.equal(c.key, 'Am');
	assert.equal(chips({ bpm: null }).bpm, null);
});

test('static guardrails', () => {
	const names = ['index.html', 'display.js', 'display-render.js', 'display.css'];
	for (const name of names) {
		const text = readFileSync(join(staticDir, name), 'utf8');
		assert.doesNotMatch(text, /https?:\/\//);
	}
	const css = readFileSync(join(staticDir, 'display.css'), 'utf8');
	assert.doesNotMatch(css, /@font-face/);
	assert.doesNotMatch(css, /@import/);
	assert.doesNotMatch(css, /url\(/);
	const jsFiles = ['display.js', 'display-render.js'];
	for (const name of jsFiles) {
		const text = readFileSync(join(staticDir, name), 'utf8');
		assert.doesNotMatch(text, /\.at\(/);
		assert.doesNotMatch(text, /Object\.hasOwn/);
		assert.doesNotMatch(text, /structuredClone/);
		assert.doesNotMatch(text, /static\s*\{/);
	}
	assert.equal(DRIFT_CYCLE.length, 8);
});
