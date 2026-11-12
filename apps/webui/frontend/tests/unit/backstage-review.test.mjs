// requirement: BACKSTAGE-08
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let rq;
const COMPONENT = readFileSync(
	new URL('../../src/routes/admin/ReviewPanel.svelte', import.meta.url),
	'utf8'
);
const PAGE = readFileSync(new URL('../../src/routes/admin/+page.svelte', import.meta.url), 'utf8');

before(async () => {
	rq = await loadTypeScriptModule('src/routes/admin/review-queue.ts');
});

test('1-4 label in order, 0 skips, unmodified only', () => {
	const labels = ['1', '2', '3', '4'].map((key) => rq.reviewKeyAction({ key }));
	assert.deepEqual(labels, [
		{ kind: 'label', label: 'correct' },
		{ kind: 'label', label: 'wrong-words' },
		{ kind: 'label', label: 'wrong-timing' },
		{ kind: 'label', label: 'not-this-song' }
	]);
	assert.deepEqual(rq.reviewKeyAction({ key: '0' }), { kind: 'skip' });
	assert.equal(rq.reviewKeyAction({ key: '5' }), null);
	assert.equal(rq.reviewKeyAction({ key: '1', metaKey: true }), null);
	assert.equal(rq.reviewKeyAction({ key: '0', ctrlKey: true }), null);
	assert.deepEqual(
		rq.REVIEW_LABELS.map((l) => l.text),
		['Correct', 'Wrong words', 'Wrong timing', 'Not this song']
	);
});

test('W/A/S/D map exactly like the arrows, modifiers included', () => {
	const pairs = [
		['w', 'ArrowUp'],
		['W', 'ArrowUp'],
		['a', 'ArrowLeft'],
		['s', 'ArrowDown'],
		['S', 'ArrowDown'],
		['d', 'ArrowRight']
	];
	for (const [letter, arrow] of pairs) {
		for (const mods of [
			{},
			{ shiftKey: true },
			{ ctrlKey: true },
			{ altKey: true },
			{ metaKey: true }
		]) {
			assert.deepEqual(
				rq.reviewKeyAction({ key: letter, ...mods }),
				rq.reviewKeyAction({ key: arrow, ...mods })
			);
		}
	}
	assert.deepEqual(rq.reviewKeyAction({ key: 's', shiftKey: true }), {
		kind: 'step',
		arrow: 'ArrowDown',
		modified: true
	});
});

test('a focused text field keeps its keys', () => {
	for (const target of [
		{ tagName: 'INPUT', type: 'text' },
		{ tagName: 'INPUT', type: 'search' },
		{ tagName: 'TEXTAREA' },
		{ tagName: 'DIV', isContentEditable: true }
	]) {
		assert.equal(rq.reviewKeyAction({ key: 'w' }, target), null);
		assert.equal(rq.reviewKeyAction({ key: '1' }, target), null);
	}
	assert.deepEqual(rq.reviewKeyAction({ key: '0' }, { tagName: 'BUTTON' }), { kind: 'skip' });
	assert.deepEqual(rq.reviewKeyAction({ key: '0' }, { tagName: 'INPUT', type: 'checkbox' }), {
		kind: 'skip'
	});
});

test('skip advance is clamped; position text', () => {
	assert.equal(rq.advanceReview(0, 3), 1);
	assert.equal(rq.advanceReview(2, 3), 2);
	assert.equal(rq.advanceReview(0, 0), -1);
	assert.equal(rq.positionText(0, 41), '1 of 41');
	assert.equal(rq.positionText(-1, 0), '0 of 0');
});

test('suspect line: most red words, earliest wins a tie, text and timing from words', () => {
	const words = ['a', 'b', 'c', 'd', 'e'].map((word, i) => ({ word, start_s: i, end_s: i + 0.5 }));
	const lines = [
		{ first_idx: 0, last_idx: 1, n_red: 1, end_s: 1.5 },
		{ first_idx: 2, last_idx: 3, n_red: 2, end_s: null },
		{ first_idx: 4, last_idx: 4, n_red: 2, end_s: 4.5 }
	];
	assert.deepEqual(rq.pickSuspectLine(words, lines), {
		text: 'c d',
		start_s: 2,
		end_s: 3.5,
		n_red: 2,
		line_no: 2
	});
	assert.equal(rq.pickSuspectLine(words, []), null);
	assert.equal(rq.fmtTime(83.25), '1:23.3');
	assert.equal(rq.fmtTime(null), '-');
});

test('labels stay inert while no label-write endpoint exists; nothing goes to localStorage', () => {
	assert.equal(rq.LABELS_WRITABLE, false);
	assert.doesNotMatch(COMPONENT, /(localStorage|sessionStorage)\s*[.[]/);
	assert.doesNotMatch(COMPONENT, /api-rb/);
	assert.match(COMPONENT, /fetchLyricTriage\(\{ order: 'suspect'/);
});

test('Cue line uses only the preview cue path, never a deck', () => {
	assert.match(COMPONENT, /import\('\$lib\/player\/preview-cue\.svelte'\)/);
	assert.doesNotMatch(COMPONENT, /audio-engine|loadDeck|loadTrack|performance-ipc/);
	assert.match(COMPONENT, /<svg[^>]*aria-hidden="true"/);
});

test('flag on swaps in ReviewPanel for ?p=triage; flag off keeps LyricTriage', () => {
	assert.match(PAGE, /panel\.id === 'triage'\}\s*<ReviewPanel \/>/);
	assert.match(PAGE, /\{#snippet triagePanel\(\)\}[\s\S]*<LyricTriage \/>/);
});

test('keys work as soon as the page lands: a window listener, no focus needed', () => {
	assert.match(COMPONENT, /<svelte:window onkeydown=\{onKeydown\} \/>/);
	assert.match(COMPONENT, /if \(event\.defaultPrevented\) return;/);
	assert.match(COMPONENT, /reviewKeyAction\(event, event\.target\)/);
});

test('the red-words explainer uses the placed tip layer, not a native title over the label row', () => {
	assert.match(COMPONENT, /data-testid="review-red-words"\s+use:tip=/);
	assert.doesNotMatch(COMPONENT, /title="Words on this line the witness distrusts"/);
});

test('a 404 detail from the words route is shown in the panel; US spelling', () => {
	assert.match(COMPONENT, /onMissing: \(detail\) => \(missing = detail\)/);
	assert.match(COMPONENT, /Server: \{lineState\.missing\}/);
	assert.doesNotMatch(COMPONENT, /labelled/i);
});
