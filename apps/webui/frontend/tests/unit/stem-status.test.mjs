// requirement STEM-48: a failed deck stem load stays recoverable after StemRow
// unmount and remount (issue #12431). This file mounts StemRow.svelte directly,
// not stem-status-view.test.mjs (pure stemStatusView mapping only).
//
// SSR via load-svelte-ssr.mjs models sequential renders as unmount+remount; it
// does not exercise client {#key} or layout-driven lazy remount. Real click path:
// StemRow.contract.ts FailedRetry (Storybook userEvent).
//
// [if] a stem load fails before the row mounts [then] STEMS FAILED - RETRY button
// [if] a failed row remounts [then] the same retry button is still present
// [if] retry is activated after remount [then] onLoad runs (invoked here, not click)
// [if] the retry succeeds [then] ready chips render without the retry button
//
import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as StemRow } from '$lib/components/rb/deck/StemRow.svelte';",
	"export { render } from 'svelte/server';",
	"export { unavailableStemDeckState, readyStemDeckState } from '$lib/rb/stem-graph';"
].join('\n');

const noop = async () => {};
const ALIGNMENT = { sample_rate_hz: 44100, frame_count: 100, channel_count: 2, duration_ms: 2 };
const DECK_ID = 1;

let mod;
let onLoadCalls = 0;

before(async () => {
	globalThis.requestAnimationFrame = () => 0;
	globalThis.cancelAnimationFrame = () => {};
	mod = await loadSvelteSsrModule(ENTRY);
	onLoadCalls = 0;
});

function decode(text) {
	return text
		.replaceAll('&quot;', '"')
		.replaceAll('&#39;', "'")
		.replaceAll('&lt;', '<')
		.replaceAll('&gt;', '>')
		.replaceAll('&amp;', '&');
}

function visibleText(html) {
	return decode(html.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, ''))
		.replace(/\s+/g, ' ')
		.trim();
}

function attr(tag, name) {
	const match = tag.match(new RegExp(`\\s${name}="([^"]*)"`));
	return match === null ? null : decode(match[1]);
}

function deckFromStems(stems) {
	return { deck_id: DECK_ID, stems };
}

function failedStems() {
	return { ...mod.unavailableStemDeckState('decode failed: bad frame'), status: 'error' };
}

function readyStems() {
	return mod.readyStemDeckState(
		{ source: 'demucs', model: 'htdemucs', layout: 'demucs4' },
		ALIGNMENT
	);
}

function stemRowProps(stems, { onLoad } = {}) {
	const load =
		onLoad ??
		(async () => {
			onLoadCalls += 1;
		});
	return {
		deck: deckFromStems(stems),
		pending: false,
		testIdScope: 'deck',
		onMute: noop,
		onSolo: noop,
		onLoad: load
	};
}

function renderStemRow(props) {
	const html = mod.render(mod.StemRow, { props }).body;
	return parseStemRow(html, 'deck');
}

function parseStemRow(html, scope) {
	const group = html.match(/<div class="stems[^"]*"[^>]*>/);
	assert.ok(group, `StemRow rendered no .stems group:\n${html}`);
	const status = html.match(
		new RegExp(
			`(<(span|button) class="status[^"]*"[^>]*data-testid="stem-status-${scope}-${DECK_ID}"[^>]*>)([\\s\\S]*?)<\\/\\2>`
		)
	);
	return {
		groupTitle: attr(group[0], 'title'),
		groupState: attr(group[0], 'data-stems-state'),
		label: status === null ? null : visibleText(status[3]),
		labelTitle: status === null ? null : attr(status[1], 'title'),
		labelTag: status === null ? null : status[2],
		labelAction: status === null ? null : attr(status[1], 'data-stems-action'),
		chipsDisabled: [...html.matchAll(/<button class="chip[^"]*"[^>]*>/g)].map((m) =>
			/\sdisabled=""/.test(m[0])
		)
	};
}

function assertFailedRetry(out) {
	assert.equal(out.groupState, 'error');
	assert.equal(out.label, 'STEMS FAILED - RETRY');
	assert.equal(out.labelTag, 'button');
	assert.equal(out.labelAction, 'retry');
	assert.equal(out.labelTitle, 'Stems failed to load: decode failed: bad frame. Click to retry.');
	assert.deepEqual(out.chipsDisabled, [true, true, true]);
}

describe('StemRow failed load survives remount (#12431)', () => {
	test('failed before first mount: STEMS FAILED - RETRY button', () => {
		const props = stemRowProps(failedStems());
		const out = renderStemRow(props);
		assertFailedRetry(out);
	});

	test('failed after remount: same retry button', () => {
		const props = stemRowProps(failedStems());
		renderStemRow(props);
		const out = renderStemRow(props);
		assertFailedRetry(out);
	});

	test('retry activatable after remount: onLoad handler runs', async () => {
		onLoadCalls = 0;
		const props = stemRowProps(failedStems());
		renderStemRow(props);
		renderStemRow(props);
		await props.onLoad();
		assert.equal(onLoadCalls, 1);
	});

	test('retry success: ready stems drop the retry status button', () => {
		const props = stemRowProps(readyStems());
		const out = renderStemRow(props);
		assert.equal(out.groupState, 'ready');
		assert.equal(out.label, null);
		assert.deepEqual(out.chipsDisabled, [false, false, false, false, false]);
	});

	test('error without onLoad on remount: no retry control, reason on group hover', () => {
		const stems = failedStems();
		const props = {
			...stemRowProps(stems),
			onLoad: undefined
		};
		renderStemRow(props);
		const out = renderStemRow(props);
		assert.equal(out.groupState, 'error');
		assert.equal(out.label, null);
		assert.equal(out.groupTitle, 'Stems failed to load: decode failed: bad frame. Click to retry.');
	});
});
