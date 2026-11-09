// Issue #9474: OJ motion policy table + frame-budget scan of OJ assistant files.
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const ASSISTANT_DIR = fileURLToPath(
	new URL('../../src/lib/components/assistant/', import.meta.url)
);
const STILL = { idleGestures: false, bubbles: false, fillMode: 'static' };
const LIVE = { idleGestures: true, bubbles: true, fillMode: 'animated' };

let anyDeckPlaying;
let ojMotionPolicy;

before(async () => {
	const policy = await loadTypeScriptModule('src/lib/components/assistant/oj-motion-policy.ts');
	anyDeckPlaying = policy.anyDeckPlaying;
	ojMotionPolicy = policy.ojMotionPolicy;
});

//----- policy

test('policy: full 16-case table', () => {
	for (let bits = 0; bits < 16; bits++) {
		const input = {
			anyDeckPlaying: !!(bits & 1),
			performanceVisible: !!(bits & 2),
			reducedMotion: !!(bits & 4),
			streaming: !!(bits & 8)
		};
		const still = input.reducedMotion || (input.anyDeckPlaying && input.performanceVisible);
		assert.deepEqual(ojMotionPolicy(input), still ? STILL : LIVE, JSON.stringify(input));
	}
});

test('if a deck plays with performance visible then gestures and bubbles are off, fill static', () => {
	assert.deepEqual(
		ojMotionPolicy({
			anyDeckPlaying: true,
			performanceVisible: true,
			reducedMotion: false,
			streaming: false
		}),
		STILL
	);
});

test('if reduced motion is on and nothing plays then still', () => {
	assert.deepEqual(
		ojMotionPolicy({
			anyDeckPlaying: false,
			performanceVisible: false,
			reducedMotion: true,
			streaming: false
		}),
		STILL
	);
});

test('if nothing plays and reduced motion is off then everything is on', () => {
	assert.deepEqual(
		ojMotionPolicy({
			anyDeckPlaying: false,
			performanceVisible: true,
			reducedMotion: false,
			streaming: true
		}),
		LIVE
	);
});

test('non-boolean input throws TypeError', () => {
	assert.throws(
		() =>
			ojMotionPolicy({
				anyDeckPlaying: 1,
				performanceVisible: false,
				reducedMotion: false,
				streaming: false
			}),
		TypeError
	);
	assert.throws(() => ojMotionPolicy({}), TypeError);
});

test('anyDeckPlaying reads deckStates shape', () => {
	assert.equal(anyDeckPlaying({ A: { playing: false }, B: { playing: true } }), true);
	assert.equal(anyDeckPlaying({ A: { playing: false }, B: { playing: false } }), false);
	assert.throws(() => anyDeckPlaying({ A: { playing: 'yes' } }), TypeError);
});

//----- frame-budget scan

const ALLOWED_KEYFRAME_PROPS = new Set(['transform', 'opacity']);

function ojFiles(dir) {
	return readdirSync(dir)
		.filter((name) => /^Oj.*\.svelte$/.test(name) || /^oj-.*\.ts$/.test(name))
		.sort();
}

function keyframeBlocks(src) {
	const blocks = [];
	const re = /@keyframes\s+[\w-]+\s*\{/g;
	let match;
	while ((match = re.exec(src))) {
		let depth = 1;
		let i = re.lastIndex;
		while (i < src.length && depth > 0) {
			if (src[i] === '{') depth++;
			else if (src[i] === '}') depth--;
			i++;
		}
		blocks.push(src.slice(re.lastIndex, i - 1));
	}
	return blocks;
}

function secondTopLevelArg(src, start) {
	const args = [];
	let depth = 0;
	let from = start;
	for (let i = start; i < src.length; i++) {
		const c = src[i];
		if ('([{'.includes(c)) depth++;
		else if (')]}'.includes(c)) {
			if (depth === 0) {
				args.push(src.slice(from, i));
				return args[1];
			}
			depth--;
		} else if (c === ',' && depth === 0) {
			args.push(src.slice(from, i));
			from = i + 1;
		}
	}
	return undefined;
}

function scanSource(name, src) {
	const hits = [];
	if (/\brequestAnimationFrame\b/.test(src)) hits.push(`${name}: requestAnimationFrame`);
	for (const m of src.matchAll(/\bsetInterval\s*\(/g)) {
		const period = /^\s*([\d_]+)\s*$/.exec(secondTopLevelArg(src, m.index + m[0].length) ?? '');
		const ms = period ? Number(period[1].replaceAll('_', '')) : NaN;
		if (!(ms >= 1000)) hits.push(`${name}: setInterval period ${period ? ms : 'unreadable'}`);
	}
	for (const block of keyframeBlocks(src)) {
		for (const decl of block.matchAll(/([a-zA-Z-]+)\s*:/g)) {
			if (!ALLOWED_KEYFRAME_PROPS.has(decl[1]))
				hits.push(`${name}: @keyframes animates ${decl[1]}`);
		}
	}
	return hits;
}

function scanDir(dir) {
	const files = ojFiles(dir);
	return {
		files,
		hits: files.flatMap((name) => scanSource(name, readFileSync(join(dir, name), 'utf8')))
	};
}

function withFixtures(fixtures, fn) {
	const dir = mkdtempSync(join(tmpdir(), 'oj-scan-'));
	try {
		for (const [name, body] of Object.entries(fixtures)) writeFileSync(join(dir, name), body);
		return fn(dir);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
}

test('scan: real assistant OJ files stay off the frame budget', () => {
	const { files, hits } = scanDir(ASSISTANT_DIR);
	assert.ok(files.includes('oj-motion-policy.ts'), `glob matched: ${files.join(', ')}`);
	assert.deepEqual(hits, []);
});

test('scan mutation: OjFixture.svelte keyframes animating height fails naming the file', () => {
	withFixtures(
		{
			'OjFixture.svelte':
				'<style>@keyframes grow { from { height: 0; } to { height: 10px; } }</style>'
		},
		(dir) => {
			const { hits } = scanDir(dir);
			assert.ok(
				hits.length > 0 && hits.every((h) => h.startsWith('OjFixture.svelte')),
				hits.join('\n')
			);
		}
	);
});

test('scan mutation: oj-x.ts calling requestAnimationFrame fails', () => {
	withFixtures({ 'oj-x.ts': 'requestAnimationFrame(() => {});' }, (dir) => {
		assert.deepEqual(scanDir(dir).hits, ['oj-x.ts: requestAnimationFrame']);
	});
});

test('scan: setInterval periods and allowed keyframes', () => {
	withFixtures(
		{
			'oj-fast.ts': 'setInterval(tick, 250);',
			'oj-slow.ts': 'setInterval(tick, 1_000);',
			'oj-var.ts': 'setInterval(tick, period);',
			'oj-arrow-fast.ts': 'setInterval(() => { go(1); }, 250);',
			'oj-arrow-slow.ts': 'setInterval(() => {}, 1_000);',
			'OjOffset.svelte': '<style>@keyframes m { to { offset: 10px; } }</style>',
			'OjOk.svelte':
				'<style>@keyframes f { 0% { transform: scale(1); opacity: 0; } 100% { opacity: 1; } }</style>',
			'Other.svelte': 'requestAnimationFrame(x);',
			'notoj.ts': 'requestAnimationFrame(x);'
		},
		(dir) => {
			assert.deepEqual(scanDir(dir).hits, [
				'OjOffset.svelte: @keyframes animates offset',
				'oj-arrow-fast.ts: setInterval period 250',
				'oj-fast.ts: setInterval period 250',
				'oj-var.ts: setInterval period unreadable'
			]);
		}
	);
});
