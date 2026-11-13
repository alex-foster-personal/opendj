/**
 * OJ branding of the agent sidebar (issue #13711): heading, aside label, glass state and the
 * juice fill, rendered through svelte/server from AgentSidebarView and OjAgentHeader.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ssr;
let header;
let policy;

before(async () => {
	header = await loadTypeScriptModule('src/lib/components/assistant/oj-agent-header.ts');
	policy = await loadTypeScriptModule('src/lib/components/assistant/oj-motion-policy.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as AgentSidebarView } from '$lib/components/assistant/AgentSidebarView.svelte';",
			"export { default as OjAgentHeader } from '$lib/components/assistant/OjAgentHeader.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const noop = () => {};
const LIVE = {
	anyDeckPlaying: false,
	performanceVisible: false,
	reducedMotion: false,
	streaming: true
};
const STREAMING_TURN = {
	items: [
		{ kind: 'user', text: 'what is on deck 1' },
		{ kind: 'text', text: 'Deck 1 has' }
	],
	busy: true,
	done: null
};

function renderView(props = {}) {
	return ssr.render(ssr.AgentSidebarView, {
		props: {
			items: [],
			busy: false,
			done: null,
			availability: 'ready',
			ontier: noop,
			onsend: noop,
			onapprove: noop,
			ondecline: noop,
			onstop: noop,
			onnewchat: noop,
			onclose: noop,
			...props
		}
	}).body;
}

function renderHeader(look) {
	return ssr.render(ssr.OjAgentHeader, { props: { look } }).body;
}

function lookFor(turn, motionInput, style = 'juice') {
	return header.ojAgentLook(turn, policy.ojMotionPolicy(motionInput), style);
}

function classOf(html, marker) {
	const tag = html.match(new RegExp(`<[^>]*${marker}[^>]*>`));
	assert.ok(tag, `no element matching ${marker}`);
	return tag[0].match(/class="([^"]*)"/)?.[1] ?? '';
}

test('agent sidebar heading and aside label name OJ', () => {
	/** [if] the agent sidebar renders [then] its h2 is OJ and the aside label names OJ and assistant, [else stop]. */
	const html = renderView();
	assert.match(html, /<h2[^>]*>\s*OJ\s*<\/h2>/);
	assert.doesNotMatch(html, /<h2[^>]*>\s*Assistant\s*<\/h2>/);
	const label = html.match(/<aside class="agent-sidebar[^"]*"[^>]*aria-label="([^"]*)"/)?.[1];
	assert.ok(label, 'agent-sidebar aside has an aria-label');
	assert.match(label, /\bOJ\b/);
	assert.match(label, /assistant/i);
	assert.match(html, /class="oj-glass idle/);
});

test('a streaming turn shows the thinking glass and the animated juice fill', () => {
	/** [if] a turn is streaming [then] the glass is in think and the juice fill is animated with bubbles, [else stop]. */
	const look = lookFor(STREAMING_TURN, LIVE);
	assert.equal(look.glass, 'think');
	assert.equal(look.fillPhase, 'streaming');
	assert.equal(look.streamedChars, 'Deck 1 has'.length);
	// Markdown needs a DOM, so the view gets only the user line; the look carries the streamed text.
	const html = renderView({ items: STREAMING_TURN.items.slice(0, 1), busy: true, oj: look });
	assert.match(html, /class="oj-glass think/);
	assert.match(html, /data-oj-fill-phase="streaming"/);
	assert.match(html, /data-oj-fill-mode="animated"/);
	assert.match(classOf(html, 'class="oj-agent-juice'), /\banimated\b/);
	assert.equal((html.match(/class="oj-bubble/g) ?? []).length, 3);
	assert.ok(Number(html.match(/data-oj-fill-level="([\d.]+)"/)[1]) > 0);
});

test('a playing deck on /performance or reduced motion keeps the fill static', () => {
	/** [if] a deck plays on /performance or reduced motion is on [then] the fill has no transition and no bubbles, [else stop]. */
	const cases = [
		{ ...LIVE, anyDeckPlaying: true, performanceVisible: true },
		{ ...LIVE, reducedMotion: true }
	];
	for (const input of cases) {
		const look = lookFor(STREAMING_TURN, input);
		assert.equal(look.fillMode, 'static', JSON.stringify(input));
		const html = renderHeader(look);
		assert.match(html, /data-oj-fill-mode="static"/);
		assert.doesNotMatch(classOf(html, 'class="oj-agent-juice'), /\banimated\b/);
		assert.doesNotMatch(html, /oj-bubble/);
		assert.match(classOf(html, 'class="oj-glass'), /\bstill\b/);
	}
});

test('the plain loading style renders no juice fill', () => {
	/** [if] the loading variant is plain [then] no juice fill or bubble renders while the glass still thinks, [else stop]. */
	const look = lookFor(STREAMING_TURN, LIVE, header.ojLoadingStyle('plain'));
	assert.equal(look.fillMode, 'none');
	const html = renderHeader(look);
	assert.doesNotMatch(html, /oj-agent-fill|oj-agent-juice|oj-bubble/);
	assert.match(html, /class="oj-glass think/);
});

test('a landed reply smiles and drains; a held or new session is idle', () => {
	/** [if] the reply lands [then] the glass smiles and the fill drains, while a held call or empty session stays idle, [else stop]. */
	const landed = lookFor({ ...STREAMING_TURN, busy: false, done: 'stop' }, LIVE);
	assert.equal(landed.glass, 'smile');
	assert.equal(landed.fillPhase, 'done');
	const held = lookFor({ ...STREAMING_TURN, busy: false, done: 'needs_confirmation' }, LIVE);
	assert.equal(held.glass, 'idle');
	assert.equal(held.fillPhase, 'idle');
	const waiting = lookFor({ items: [{ kind: 'user', text: 'hi' }], busy: true, done: null }, LIVE);
	assert.equal(waiting.fillPhase, 'waiting');
});

test('the loading variant defaults to the juice fill and rejects unknown values', () => {
	/** [if] the loading variant is read [then] its default is juice and an unknown value throws, [else stop]. */
	assert.equal(header.OJ_LOADING_VARIANT.id, 'variant_anim_oj_loading');
	assert.equal(header.OJ_LOADING_VARIANT.defaultValue, 'juice');
	assert.deepEqual(
		header.OJ_LOADING_VARIANT.variants.map((v) => v.value),
		['juice', 'plain']
	);
	assert.throws(() => header.ojLoadingStyle('fizzy'), /juice\|plain/);
});
