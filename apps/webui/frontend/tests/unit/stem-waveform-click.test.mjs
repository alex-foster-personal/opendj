// requirement: STEMPERF-11710
// [if] a stem row click also starts a seek on the main waveform [then] isolating a stem jumps the playhead, [else stop].
// [if] an inert row dispatches [then] a click on a stem the stem-set does not have sends a command the graph refuses, [else stop].
/**
 * Issue #10317: click a stem mini-waveform row to solo that stem (exclusive),
 * Shift+click to mute it. The click never reaches the main waveform's seek,
 * and a row the stem-set's layout cannot drive stays inert.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';
import { bundleStemWaveRowViewClient } from './mount-svelte-client.mjs';
import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ALL = ['vocal', 'instrumental', 'drums', 'bass', 'other'];
const deck = (controls, extra = {}) => ({
	deck_id: 2,
	stems: {
		status: 'ready',
		layout: 'demucs4',
		available_controls: ALL,
		error: null,
		controls,
		...extra
	}
});
const ctl = (drums) => ({ drums: { muted: false, solo: false, ...drums } });

let ui;
let client;
let teardownDom;

before(async () => {
	ui = await loadTypeScriptModule('src/lib/components/rb/wave/stem-waveform-ui.ts');
	teardownDom = installQualityBadgeDom();
	client = await bundleStemWaveRowViewClient();
});

after(() => {
	teardownDom?.();
});

function findByTestId(root, testId) {
	if (root.getAttribute?.('data-testid') === testId) return root;
	for (const child of root.childNodes ?? []) {
		const hit = findByTestId(child, testId);
		if (hit) return hit;
	}
	return null;
}

function fire(node, type, shiftKey = false, timeStamp = 123) {
	node.dispatchEvent({
		type,
		bubbles: true,
		cancelBubble: false,
		defaultPrevented: false,
		shiftKey,
		timeStamp,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {
			this.cancelBubble = true;
		}
	});
}

test('plain click yields exclusive stem_solo toggling the current solo', () => {
	assert.deepEqual(ui.stemWaveRowClickCommand(deck(ctl({})), 'drums', false), {
		type: 'stem_solo',
		deck: 2,
		stem: 'drums',
		solo: true,
		exclusive: true
	});
	assert.deepEqual(ui.stemWaveRowClickCommand(deck(ctl({ solo: true })), 'drums', false), {
		type: 'stem_solo',
		deck: 2,
		stem: 'drums',
		solo: false,
		exclusive: true
	});
});

test('Shift+click yields stem_mute toggling the current mute', () => {
	assert.deepEqual(ui.stemWaveRowClickCommand(deck(ctl({})), 'drums', true), {
		type: 'stem_mute',
		deck: 2,
		stem: 'drums',
		muted: true
	});
	assert.deepEqual(ui.stemWaveRowClickCommand(deck(ctl({ muted: true })), 'drums', true), {
		type: 'stem_mute',
		deck: 2,
		stem: 'drums',
		muted: false
	});
});

test('StemWaveRowView SSR exposes the stem-wave-row testid', async () => {
	const bundle = await bundleSvelteEntry(`
		export { default as StemWaveRowView } from '$lib/components/rb/wave/StemWaveRowView.svelte';
	`);
	const html = await renderToHtml(bundle.StemWaveRowView, {
		stem: 'vocal',
		visible: true,
		unavailableTip: null,
		deckId: 1,
		controlState: { muted: false, solo: false }
	});
	assert.match(html, /data-testid="stem-wave-row-vocal-deck-1"/);
});

test('an unavailable stem yields null', () => {
	const roformer = deck(ctl({}), {
		layout: 'roformer2',
		available_controls: ['vocal', 'instrumental']
	});
	const missing = deck(ctl({}), { status: 'missing' });
	for (const shift of [false, true]) {
		assert.equal(ui.stemWaveRowClickCommand(roformer, 'drums', shift), null);
		assert.equal(ui.stemWaveRowClickCommand(missing, 'drums', shift), null);
	}
});

function mountRow(props) {
	const { StemWaveRowView, mount, flushSync } = client;
	const outer = document.createElement('div');
	const target = document.createElement('div');
	outer.appendChild(target);
	document.body.appendChild(outer);
	const parent = [];
	outer.addEventListener('click', () => parent.push('click'));
	outer.addEventListener('pointerdown', () => parent.push('pointerdown'));
	const solo = [];
	const mute = [];
	const instance = mount(StemWaveRowView, {
		target,
		props: {
			visible: true,
			deckId: 1,
			onSolo: (t) => solo.push(t),
			onMute: (t) => mute.push(t),
			...props
		}
	});
	flushSync();
	return {
		outer,
		parent,
		solo,
		mute,
		done: () => {
			client.unmount(instance);
			outer.remove();
		}
	};
}

test('a click on a live row dispatches once and does not reach the seek parent', () => {
	const r = mountRow({
		stem: 'vocal',
		unavailableTip: null,
		controlState: { muted: false, solo: false }
	});
	try {
		const row = findByTestId(r.outer, 'stem-wave-row-vocal-deck-1');
		assert.ok(row, 'row testid');
		assert.equal(row.getAttribute('role'), 'button');
		assert.match(row.getAttribute('aria-label'), /vocal/);
		assert.match(row.getAttribute('aria-label'), /solo/);
		fire(row, 'click');
		client.flushSync();
		assert.deepEqual(r.solo, [123]);
		assert.deepEqual(r.mute, []);
		assert.deepEqual(r.parent, []);
		fire(row, 'click', true, 456);
		client.flushSync();
		assert.deepEqual(r.solo, [123]);
		assert.deepEqual(r.mute, [456]);
		fire(row, 'pointerdown');
		assert.deepEqual(r.parent, []);
	} finally {
		r.done();
	}
});

test('aria-pressed follows controls[stem].solo', () => {
	for (const solo of [true, false]) {
		const r = mountRow({
			stem: 'drums',
			unavailableTip: null,
			controlState: { muted: false, solo }
		});
		try {
			const row = findByTestId(r.outer, 'stem-wave-row-drums-deck-1');
			assert.equal(String(row.getAttribute('aria-pressed')), String(solo));
		} finally {
			r.done();
		}
	}
});

test('an inert row keeps pointer-events: none and dispatches nothing', () => {
	const r = mountRow({
		stem: 'drums',
		unavailableTip: 'DRUMS is not a separate stem in this roformer2 bundle',
		controlState: { muted: false, solo: false }
	});
	try {
		const row = findByTestId(r.outer, 'stem-wave-row-drums-deck-1');
		assert.ok(row.classList.contains('inert'));
		assert.equal(String(row.getAttribute('data-unavailable')), 'true');
		fire(row, 'click');
		fire(row, 'click', true);
		client.flushSync();
		assert.deepEqual(r.solo, []);
		assert.deepEqual(r.mute, []);
		assert.equal(row.getAttribute('aria-disabled'), 'true');
	} finally {
		r.done();
	}
});
