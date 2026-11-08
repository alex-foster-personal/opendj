/**
 * OJ setup-sidebar juice fill helper and AssistantSidebarView fill layer (issue #9476).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, mock, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const VIEW_PATH = fileURLToPath(
	new URL('../../src/lib/components/assistant/AssistantSidebarView.svelte', import.meta.url)
);

let fill;
let ssr;

const baseViewProps = {
	visible: true,
	mode: 'chat',
	status: { configured: true, model: 'm' },
	statusError: null,
	messages: [],
	sendError: null,
	canSend: false,
	draft: '',
	send: async () => {},
	onStop: () => {}
};

function readViewStyle() {
	const source = readFileSync(VIEW_PATH, 'utf8');
	const match = source.match(/<style>([\s\S]*?)<\/style>/);
	assert.ok(match, 'AssistantSidebarView has a style block');
	return match[1];
}

function transitionPropertyLists(style) {
	const lists = [];
	for (const decl of style.matchAll(/transition\s*:\s*([^;{}]+);/g)) {
		lists.push(decl[1].trim());
	}
	return lists;
}

function keyframeBlocks(style) {
	const blocks = [];
	for (const match of style.matchAll(/@keyframes\s+([\w-]+)\s*\{([\s\S]*?)\n\t\}/g)) {
		blocks.push({ name: match[1], body: match[2] });
	}
	return blocks;
}

function propsInKeyframeStep(stepBody) {
	const props = new Set();
	for (const decl of stepBody.matchAll(/([a-z-]+)\s*:/g)) {
		props.add(decl[1]);
	}
	return props;
}

before(async () => {
	fill = await loadTypeScriptModule('src/lib/components/assistant/oj-fill.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as AssistantSidebarView } from '$lib/components/assistant/AssistantSidebarView.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

test('waiting phase sets level to 0.3 regardless of streamed character count', () => {
	assert.equal(fill.ojFillLevel({ phase: 'waiting', streamedChars: 0 }), 0.3);
	assert.equal(fill.ojFillLevel({ phase: 'waiting', streamedChars: 500 }), 0.3);
});

test('streaming level rises with character count and never exceeds 0.9', () => {
	const l0 = fill.ojFillLevel({ phase: 'streaming', streamedChars: 0 });
	const l200 = fill.ojFillLevel({ phase: 'streaming', streamedChars: 200 });
	const l2000 = fill.ojFillLevel({ phase: 'streaming', streamedChars: 2000 });
	assert.ok(l0 < l200 && l200 < l2000);
	assert.ok(l0 <= 0.9 && l200 <= 0.9 && l2000 <= 0.9);
	assert.ok(fill.ojFillLevel({ phase: 'streaming', streamedChars: 1e7 }) <= 0.9);
	assert.equal(l0, fill.OJ_FILL_WAITING);
});

test('done phase drains to 0 after 600 ms of fake time', (t) => {
	t.mock.timers.enable({ apis: ['Date'] });
	try {
		const start = Date.now();
		const atStream = fill.ojStreamingLevel(2000);
		assert.equal(
			fill.ojFillLevel({
				phase: 'done',
				streamedChars: 2000,
				elapsedMs: Date.now() - start
			}),
			atStream
		);
		t.mock.timers.tick(300);
		const mid = fill.ojFillLevel({
			phase: 'done',
			streamedChars: 2000,
			elapsedMs: Date.now() - start
		});
		assert.ok(mid > 0 && mid < atStream);
		t.mock.timers.tick(300);
		assert.equal(
			fill.ojFillLevel({
				phase: 'done',
				streamedChars: 2000,
				elapsedMs: Date.now() - start
			}),
			0
		);
		t.mock.timers.tick(1000);
		assert.equal(
			fill.ojFillLevel({
				phase: 'done',
				streamedChars: 2000,
				elapsedMs: Date.now() - start
			}),
			0
		);
	} finally {
		t.mock.timers.reset();
	}
	assert.equal(fill.ojFillLevel({ phase: 'done', streamedChars: 2000 }), 0);
	assert.equal(fill.ojFillTransitionMs('done'), fill.OJ_FILL_DRAIN_MS);
	assert.equal(fill.OJ_FILL_DRAIN_MS, 600);
	assert.equal(fill.ojFillLevel({ phase: 'idle', streamedChars: 0 }), 0);
});

test('static fillMode SSR: no animated class, no bubbles, and helper hides bubbles', () => {
	const body = ssr.render(ssr.AssistantSidebarView, {
		props: {
			...baseViewProps,
			streaming: 'hello',
			fillPhase: 'streaming',
			fillMode: 'static'
		}
	}).body;
	assert.match(body, /data-oj-fill-mode="static"/);
	const staticFillTag = body.match(
		/<div[^>]*class="[^"]*oj-fill[^"]*"[^>]*data-oj-fill-mode="static"/
	);
	assert.ok(staticFillTag, 'static fill div');
	assert.doesNotMatch(staticFillTag[0], /\banimated\b/);
	assert.doesNotMatch(body, /oj-bubble/);
	assert.equal(fill.ojFillShowsBubbles('static', 'streaming'), false);

	const animated = ssr.render(ssr.AssistantSidebarView, {
		props: {
			...baseViewProps,
			streaming: 'hello',
			fillPhase: 'streaming',
			fillMode: 'animated'
		}
	}).body;
	const animatedFillTag = animated.match(/<div[^>]*class="[^"]*oj-fill[^"]*animated[^"]*"[^>]*>/);
	assert.ok(animatedFillTag, 'animated fill div');
	assert.equal((animated.match(/oj-bubble/g) ?? []).length, 3);

	const none = ssr.render(ssr.AssistantSidebarView, {
		props: {
			...baseViewProps,
			streaming: 'hello',
			fillPhase: 'streaming',
			fillMode: 'none'
		}
	}).body;
	assert.doesNotMatch(none, /oj-fill/);
});

test('static fillMode: every transition rule in source targets animated fill or is none', () => {
	const style = readViewStyle();
	for (const list of transitionPropertyLists(style)) {
		const idx = style.indexOf(`transition: ${list};`);
		const slice = style.slice(Math.max(0, idx - 120), idx + 40);
		assert.ok(
			slice.includes('.oj-fill.animated') ||
				(list === 'none' && slice.includes('prefers-reduced-motion')),
			`transition "${list}" is not scoped to animated fill: ${slice}`
		);
	}
});

test('fill layer source scan: only transform transitions and transform keyframes, no caret', () => {
	const source = readFileSync(VIEW_PATH, 'utf8');
	const style = readViewStyle();
	assert.doesNotMatch(source, /class="caret"/);
	assert.doesNotMatch(source, /@keyframes blink/);
	assert.match(style, /transform:\s*translateY\(/);

	for (const list of transitionPropertyLists(style)) {
		assert.ok(
			list === 'transform var(--oj-fill-ms, 0ms) linear' || list === 'none',
			`unexpected transition: ${list}`
		);
		assert.doesNotMatch(list, /\ball\b/);
	}
	for (const { name, body } of keyframeBlocks(style)) {
		for (const step of body.split(/\n\t\t/)) {
			if (!step.includes('{')) continue;
			const props = propsInKeyframeStep(step);
			for (const prop of props) {
				assert.equal(prop, 'transform', `${name} step declares ${prop}, not transform`);
			}
		}
	}
});

test('waiting render exposes screen-reader text and streaming counter stays', () => {
	const waiting = ssr.render(ssr.AssistantSidebarView, {
		props: { ...baseViewProps, streaming: '', fillPhase: 'waiting' }
	}).body;
	assert.match(waiting, /role="log"[\s\S]*oj-sr-only[\s\S]*Assistant is answering/);

	const streaming = ssr.render(ssr.AssistantSidebarView, {
		props: { ...baseViewProps, streaming: 'hello', fillPhase: 'streaming' }
	}).body;
	assert.match(streaming, /5 chars/);
});

test('ojFillLevel throws on an unknown phase', () => {
	assert.throws(
		() => fill.ojFillLevel({ phase: 'bogus', streamedChars: 0 }),
		/unknown OJ fill phase/
	);
});
