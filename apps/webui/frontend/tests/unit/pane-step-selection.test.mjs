import assert from 'node:assert/strict';
import { after, before, describe, it } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * The pane tab's previous and next buttons (issue #9744) (rekordbox browse up and down, mimicked).
 *
 * [if] next or previous skips a row, or runs past an end [then] the stepper loses the DJ's place - broken.
 * [if] with no selection next is not the first row and previous not the last [then] the first press is lost - broken.
 * [if] the stepped row gets no scroll target [then] the selection moves off screen - broken.
 * [if] the buttons stay inert or keep the not-built line [then] the controls still lie - broken.
 * [if] browser_step_selection misses the adapter or takes a delta other than -1 or 1 [then] agents lose parity - broken.
 */

let selection;
let scroll;
let explainers;
let ipc;
let ssr;

before(async () => {
	selection = await loadTypeScriptModule('src/lib/components/rb/browser/browser-midi-selection.ts');
	scroll = await loadTypeScriptModule('src/lib/components/rb/browser/browse-scroll.ts');
	explainers = await loadTypeScriptModule('src/lib/rb/planned-explainers.ts');
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as PaneTabs } from '$lib/components/rb/browser/PaneTabs.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const ROWS = ['a', 'b', 'c'].map((id, order) => ({ stable_id: id, order }));
const TAB = { title: 'House', count: 3, loading: false, sticky: false, ephemeral: false };

function _step(selectedId, delta) {
	return selection.nextMidiSelection(ROWS, selectedId, delta)?.stable_id ?? null;
}

function _renderTabs(props) {
	return ssr.render(ssr.PaneTabs, {
		props: {
			tabs: [TAB],
			active: 0,
			onactivate: () => {},
			ontogglesticky: () => {},
			onreorder: () => {},
			ondropplaylist: () => {},
			onsaveas: () => {},
			...props
		}
	}).body;
}

function _stepButton(html, label) {
	const match = html.match(new RegExp(`<button[^>]*aria-label="${label}"[^>]*>`));
	assert.ok(match, `no ${label} button rendered`);
	return match[0];
}

describe('pane step selection', () => {
	it('next moves one row down and previous one row up, clamped at both ends', () => {
		/** [if] b is selected [then] next gives c, previous a, and c stays c on next and a stays a on previous, [else stop]. */
		assert.equal(_step('b', 1), 'c');
		assert.equal(_step('b', -1), 'a');
		assert.equal(_step('c', 1), 'c');
		assert.equal(_step('a', -1), 'a');
	});

	it('with no selection next selects the first row and previous the last', () => {
		/** [if] nothing is selected [then] next selects a and previous selects c, [else stop]. */
		assert.equal(_step(null, 1), 'a');
		assert.equal(_step(null, -1), 'c');
	});

	it('the stepped row gets a scroll target, so it scrolls into view', () => {
		/** [if] the step lands on row 40 of 50 [then] the table gets a scrollTop that brings row 40 on screen, [else stop]. */
		const rows = Array.from({ length: 50 }, (_, order) => ({ order }));
		const top = scroll.browseScrollTarget({
			request: { order: 40, direction: 1, revision: 1 },
			rows,
			viewportHeight: 200,
			rowHeight: 20,
			headerOffsetPx: 0,
			slotOfRow: (index) => index
		});
		assert.ok(
			top !== null && top <= 40 * 20 && top + 200 >= 41 * 20,
			`row 40 not in view at ${top}`
		);
	});
});

describe('pane stepper buttons', () => {
	it('are live with the built explainer once the panel passes onstep, and disabled without it', () => {
		/** [if] PaneTabs gets onstep [then] both chevrons render enabled with the built tooltip, [else stop]. */
		const live = _renderTabs({ onstep: () => {} });
		for (const [label, id] of [
			['previous track in pane', 'pane-prev-track'],
			['next track in pane', 'pane-next-track']
		]) {
			const button = _stepButton(live, label);
			assert.doesNotMatch(button, /disabled|rb-inert/);
			assert.ok(button.includes(`title="${explainers.builtTitle(id)}"`), `${id} title`);
			assert.ok(!button.includes(explainers.NOT_BUILT_MARK));
		}
		assert.match(_stepButton(_renderTabs({}), 'next track in pane'), /disabled/);
	});

	it('both entries moved from the planned to the built catalogue', () => {
		/** [if] pane-prev-track or pane-next-track is still planned or not built [then] the explainer lies, [else stop]. */
		for (const id of ['pane-prev-track', 'pane-next-track']) {
			assert.equal(explainers.PLANNED_CONTROLS[id], undefined, `${id} still planned`);
			assert.ok(explainers.BUILT_CONTROLS[id].length > 25, `${id} not built`);
		}
	});
});

describe('browser_step_selection performance command', () => {
	let uninstall;
	before(() => {
		globalThis.window = {};
		uninstall = ipc.installPerformanceBrowserIpc();
	});
	after(() => {
		uninstall();
		delete globalThis.window;
	});

	it('hands the delta to the browser adapter, and refuses a bad delta or no panel', async () => {
		/** [if] an agent sends delta 1 then -1 [then] the panel steps 1 then -1; delta 2 and no panel are refused, [else stop]. */
		const steps = [];
		const unregister = ipc.registerPerformanceBrowserAdapter({
			selectPlaylist: async () => {},
			setRelatedActive: () => {},
			stepSelection: (delta) => steps.push(delta),
			readSnapshot: () => ({
				search: null,
				sort: null,
				selected_row: 'b',
				load: null,
				related_active: false
			})
		});
		try {
			await ipc.dispatchPerformanceCommand({ type: 'browser_step_selection', delta: 1 });
			const got = await ipc.dispatchPerformanceCommand({
				type: 'browser_step_selection',
				delta: -1
			});
			assert.deepEqual(steps, [1, -1]);
			assert.equal(got.browser.selected_row, 'b');
			await assert.rejects(
				ipc.dispatchPerformanceCommand({ type: 'browser_step_selection', delta: 2 }),
				/delta must be -1 or 1/
			);
			assert.deepEqual(steps, [1, -1], 'a refused step moves nothing');
		} finally {
			unregister();
		}
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'browser_step_selection', delta: 1 }),
			/requires a mounted browser panel/
		);
	});
});
