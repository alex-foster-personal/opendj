// Spec LIBM-96, issue #13731: edge auto-scroll while dragging over the
// playlist tree. Band and cap mimicked from Chromium (40 px, 20 px per frame).
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let auto;
let ssr;

before(async () => {
	auto = await loadTypeScriptModule('src/lib/rb/drag-edge-autoscroll.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as PlaylistTreeView } from '$lib/components/rb/browser/PlaylistTreeView.svelte';",
			"export { default as meta } from '$lib/components/rb/browser/PlaylistTreeView.stories';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

/** A 400 px tall container at y 100..500 over 2000 px of content, scrolled to `scrollTop`. */
function rect(scrollTop = 500) {
	return { top: 100, bottom: 500, scrollTop, scrollHeight: 2000, clientHeight: 400 };
}

/** Manual animation frames: nothing runs until `flush`, and pending handles are visible. */
function manualFrames() {
	const pending = new Map();
	let next = 1;
	return {
		request: (cb) => {
			const h = next++;
			pending.set(h, cb);
			return h;
		},
		cancel: (h) => pending.delete(h),
		flush() {
			const cbs = [...pending.values()];
			pending.clear();
			for (const cb of cbs) cb();
		},
		get pendingCount() {
			return pending.size;
		}
	};
}

function fakeScroller(scrollTop = 500) {
	return {
		scrollTop,
		scrollHeight: 2000,
		clientHeight: 400,
		getBoundingClientRect: () => ({ top: 100, bottom: 500 })
	};
}

test('[if] the pointer is in the middle of the container [then] the delta is 0, [else stop].', () => {
	assert.equal(auto.edgeAutoscrollDelta(300, rect()), 0);
	assert.equal(auto.edgeAutoscrollDelta(100 + auto.EDGE_AUTOSCROLL.bandPx, rect()), 0);
});

test('[if] the pointer is 10 px from the top edge [then] the delta is negative and larger than at 30 px, [else stop].', () => {
	const at10 = auto.edgeAutoscrollDelta(110, rect());
	const at30 = auto.edgeAutoscrollDelta(130, rect());
	assert.ok(at10 < 0, `delta at 10 px is ${at10}`);
	assert.ok(at30 < 0, `delta at 30 px is ${at30}`);
	assert.ok(Math.abs(at10) > Math.abs(at30), `${at10} vs ${at30}`);
});

test('[if] the pointer is past the bottom edge [then] the delta is capped at +20 px per frame, [else stop].', () => {
	assert.equal(auto.EDGE_AUTOSCROLL.maxStepPx, 20);
	assert.equal(auto.EDGE_AUTOSCROLL.bandPx, 40);
	assert.equal(auto.edgeAutoscrollDelta(520, rect()), 20);
	assert.equal(auto.edgeAutoscrollDelta(5000, rect()), 20);
	assert.equal(auto.edgeAutoscrollDelta(-5000, rect()), -20);
});

test('[if] the container is already scrolled to the bottom [then] a downward delta is clamped to 0, [else stop].', () => {
	assert.equal(auto.edgeAutoscrollDelta(495, rect(1600)), 0);
	assert.equal(auto.edgeAutoscrollDelta(495, rect(1595)), 5, 'only the remaining 5 px');
	assert.equal(auto.edgeAutoscrollDelta(105, rect(0)), 0, 'and an upward one at the top');
});

test('[if] the driver is stopped [then] no further animation frame is requested, [else stop].', () => {
	const frames = manualFrames();
	const el = fakeScroller();
	const driver = auto.createEdgeAutoscroll(() => el, frames);
	driver.update(495);
	assert.equal(frames.pendingCount, 1);
	frames.flush();
	assert.ok(el.scrollTop > 500, 'one frame scrolled down');
	assert.equal(frames.pendingCount, 1, 'and asked for the next');
	driver.stop();
	assert.equal(frames.pendingCount, 0);
	assert.equal(driver.running, false);
	frames.flush();
	assert.equal(frames.pendingCount, 0);
});

test('[if] the pointer moves out of the band mid-scroll [then] the loop ends by itself, [else stop].', () => {
	const frames = manualFrames();
	const el = fakeScroller();
	const driver = auto.createEdgeAutoscroll(() => el, frames);
	driver.update(105);
	frames.flush();
	assert.ok(el.scrollTop < 500);
	driver.update(300);
	frames.flush();
	assert.equal(frames.pendingCount, 0);
	assert.equal(driver.running, false);
});

test('[if] the playlist tree renders [then] its scroll container still mounts with the autoscroll wiring, [else stop].', () => {
	const body = ssr.render(ssr.PlaylistTreeView, { props: { ...ssr.meta.args } }).body;
	assert.match(body, /class="tree-scroll/);
	assert.match(body, /data-testid="playlist-row"/);
});
