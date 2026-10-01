/**
 * Pin placement against the UI (feedback-pin-position.ts, Thu 1 Oct 2026):
 * a pin is tagged to the element it was dropped on and, when that element is
 * lost, stays in place against a nearby stable anchor, and only then falls
 * back to its stored viewport percent.
 *
 * Regression lines:
 * - if a pin saved before capture existed draws anywhere but x_pct/y_pct, or
 *   with a different style string, then old pins moved and the maintainer's record broke
 * - if a pin whose element still resolves draws on a nearby anchor instead,
 *   then the fallback order is wrong
 * - if a pin whose element is gone draws at its old viewport percent while a
 *   nearby anchor still resolves, then the anchor tier is dead
 * - if an ambiguous (two matches) or hidden (0x0) selector counts as resolved,
 *   then a pin jumps onto the wrong element instead of falling through
 * - if a pin on its best captured tier is marked as a fallback, the subtle
 *   ring stops meaning "the UI moved under this pin"
 * - if capture records an element offset for a click outside the element, a
 *   fourth nearby anchor, or the primary as its own neighbour, then the saved
 *   record re-places the pin wrongly
 * - if a parked draft loses its placement across a refresh, the saved pin is
 *   silently viewport-only
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let pos;
let restore;
let feedback;

before(async () => {
	pos = await loadTypeScriptModule('src/lib/rb/feedback-pin-position.ts');
	restore = await loadTypeScriptModule('src/lib/rb/feedback-pin-draft-restore.ts');
	feedback = await loadTypeScriptModule('src/lib/rb/feedback.ts');
});

const VIEWPORT = { w: 1000, h: 800 };

/** A fake DOM: selector -> rect. Counts calls so "never measured" is assertable. */
function dom(boxes) {
	const calls = [];
	const measure = (selector) => {
		calls.push(selector);
		return boxes[selector] ?? null;
	};
	return { measure, calls };
}

const CAPTURED = {
	x_pct: 50,
	y_pct: 50,
	anchor: '#deck-a',
	element_offset: { dx_pct: 25, dy_pct: 50 },
	nearby_anchors: [
		{ selector: '[data-testid="gone"]', dx_px: 1, dy_px: 1 },
		{ selector: '[data-testid="mixer"]', dx_px: -20, dy_px: 30 },
		{ selector: '#topbar', dx_px: 400, dy_px: 400 }
	]
};

describe('resolvePinPosition tiers', () => {
	it('an old pin with no capture data draws exactly at x_pct/y_pct, unmarked, never measured', () => {
		const old = { x_pct: 12.5, y_pct: 40, anchor: '#deck-a' };
		const { measure, calls } = dom({ '#deck-a': { left: 0, top: 0, width: 100, height: 100 } });
		const r = pos.resolvePinPosition(old, measure, VIEWPORT);
		assert.equal(r.tier, 'viewport');
		assert.equal(r.fallback, false, 'an old pin was marked as on a fallback');
		assert.equal(r.via, null);
		assert.deepEqual([r.x, r.y, r.x_pct, r.y_pct], [125, 320, 12.5, 40]);
		assert.deepEqual(calls, [], 'an old pin was re-resolved against its diagnostic anchor');
		assert.equal(pos.resolvedPinStyle(old, r), feedback.pinStyle(old), 'old pin style string changed');
	});

	it('an old pin carrying null/empty capture fields (dumped by a newer daemon) still draws as before', () => {
		const old = { x_pct: 3, y_pct: 97, anchor: null, element_offset: null, nearby_anchors: [] };
		const r = pos.resolvePinPosition(old, dom({}).measure, VIEWPORT);
		assert.equal(r.tier, 'viewport');
		assert.equal(r.fallback, false);
		assert.equal(pos.resolvedPinStyle(old, r), 'left:3%;top:97%');
	});

	it('tier 1: the element still resolves, so the pin sits on it at its element offset', () => {
		const { measure } = dom({
			'#deck-a': { left: 100, top: 200, width: 400, height: 100 },
			'[data-testid="mixer"]': { left: 0, top: 0, width: 50, height: 50 }
		});
		const r = pos.resolvePinPosition(CAPTURED, measure, VIEWPORT);
		assert.equal(r.tier, 'element');
		assert.equal(r.fallback, false);
		assert.equal(r.via, '#deck-a');
		assert.deepEqual([r.x, r.y], [200, 250]);
		assert.deepEqual([r.x_pct, r.y_pct], [20, 31.25]);
		assert.equal(pos.resolvedPinStyle(CAPTURED, r), 'left:200px;top:250px');
	});

	it('tier 1 wins over tier 2 even when both resolve (fallback order)', () => {
		const { measure, calls } = dom({
			'#deck-a': { left: 100, top: 200, width: 400, height: 100 },
			'[data-testid="gone"]': { left: 900, top: 700, width: 10, height: 10 },
			'[data-testid="mixer"]': { left: 600, top: 100, width: 50, height: 50 }
		});
		const r = pos.resolvePinPosition(CAPTURED, measure, VIEWPORT);
		assert.equal(r.tier, 'element');
		assert.deepEqual(calls, ['#deck-a'], 'anchors were measured although the element resolved');
	});

	it('tier 2: element gone, the FIRST nearby anchor that still resolves places it, marked fallback', () => {
		const { measure, calls } = dom({
			'[data-testid="mixer"]': { left: 600, top: 100, width: 50, height: 50 },
			'#topbar': { left: 0, top: 0, width: 1000, height: 40 }
		});
		const r = pos.resolvePinPosition(CAPTURED, measure, VIEWPORT);
		assert.equal(r.tier, 'anchor');
		assert.equal(r.fallback, true);
		assert.equal(r.via, '[data-testid="mixer"]');
		assert.deepEqual([r.x, r.y], [580, 130]);
		assert.deepEqual(calls, ['#deck-a', '[data-testid="gone"]', '[data-testid="mixer"]']);
	});

	it('tier 3: nothing resolves, so it falls back to x_pct/y_pct, marked fallback', () => {
		const r = pos.resolvePinPosition(CAPTURED, dom({}).measure, VIEWPORT);
		assert.equal(r.tier, 'viewport');
		assert.equal(r.fallback, true);
		assert.deepEqual([r.x, r.y, r.x_pct, r.y_pct], [500, 400, 50, 50]);
		assert.equal(pos.resolvedPinStyle(CAPTURED, r), 'left:50%;top:50%');
	});

	it('a hidden (0x0) element is not resolved; it falls through to the next tier', () => {
		const { measure } = dom({
			'#deck-a': { left: 0, top: 0, width: 0, height: 0 },
			'#topbar': { left: 10, top: 10, width: 1000, height: 40 }
		});
		const r = pos.resolvePinPosition(CAPTURED, measure, VIEWPORT);
		assert.equal(r.tier, 'anchor');
		assert.equal(r.via, '#topbar');
	});

	it('an anchor-only capture resolving on its anchor is NOT a fallback', () => {
		const pin = { x_pct: 1, y_pct: 1, anchor: '.x', element_offset: null, nearby_anchors: [CAPTURED.nearby_anchors[1]] };
		const r = pos.resolvePinPosition(pin, dom({ '[data-testid="mixer"]': { left: 600, top: 100, width: 5, height: 5 } }).measure, VIEWPORT);
		assert.equal(r.tier, 'anchor');
		assert.equal(r.fallback, false);
	});

	it('an element offset with no anchor selector cannot claim tier 1', () => {
		const pin = { ...CAPTURED, anchor: null };
		const r = pos.resolvePinPosition(pin, dom({ '#topbar': { left: 0, top: 0, width: 10, height: 10 } }).measure, VIEWPORT);
		assert.equal(r.tier, 'anchor');
	});

	it('refuses a fake viewport instead of dividing by zero', () => {
		assert.throws(() => pos.resolvePinPosition(CAPTURED, dom({}).measure, { w: 0, h: 800 }), /real viewport/);
	});
});

describe('buildPinPlacement', () => {
	const primary = { selector: '#deck-a', rect: { left: 100, top: 200, width: 400, height: 100 } };

	it('records the click inside the primary box as 0..100 percent', () => {
		const p = pos.buildPinPlacement({ x: 200, y: 250 }, primary, []);
		assert.deepEqual(p.element_offset, { dx_pct: 25, dy_pct: 50 });
		assert.deepEqual(p.nearby_anchors, []);
	});

	it('records no element offset for a click outside the primary box', () => {
		const p = pos.buildPinPlacement({ x: 50, y: 250 }, primary, []);
		assert.equal(p.element_offset, null);
	});

	it('keeps at most three unique neighbours, closest offset first, never the primary', () => {
		const n = (selector, left, top, w = 10, h = 10) => ({ selector, rect: { left, top, width: w, height: h } });
		const p = pos.buildPinPlacement({ x: 200, y: 250 }, primary, [
			n('#deck-a', 190, 240),
			n('#far', 900, 700),
			n('#near', 195, 245),
			n('#near', 0, 0),
			n('#mid', 150, 200),
			n('#hidden', 199, 249, 0, 0),
			n('#midish', 100, 100)
		]);
		assert.deepEqual(
			p.nearby_anchors.map((a) => a.selector),
			['#near', '#mid', '#midish']
		);
		assert.deepEqual(p.nearby_anchors[0], { selector: '#near', dx_px: 5, dy_px: 5 });
	});
});

describe('parsePinPlacement and the parked draft', () => {
	const placement = {
		element_offset: { dx_pct: 25, dy_pct: 50 },
		nearby_anchors: [{ selector: '#mixer', dx_px: -3.5, dy_px: 12 }]
	};

	it('round-trips a well-formed placement and rejects half-shaped ones', () => {
		assert.deepEqual(pos.parsePinPlacement(placement), placement);
		assert.deepEqual(pos.parsePinPlacement({ element_offset: null, nearby_anchors: [] }), {
			element_offset: null,
			nearby_anchors: []
		});
		assert.equal(pos.parsePinPlacement(undefined), null);
		assert.equal(pos.parsePinPlacement({ element_offset: { dx_pct: 'x', dy_pct: 1 }, nearby_anchors: [] }), null);
		assert.equal(pos.parsePinPlacement({ element_offset: null, nearby_anchors: [{ selector: '#a' }] }), null);
		const four = Array.from({ length: 4 }, (_, i) => ({ selector: `#a${i}`, dx_px: 0, dy_px: 0 }));
		assert.equal(pos.parsePinPlacement({ element_offset: null, nearby_anchors: four }), null);
	});

	it('a refresh mid-draft keeps the placement; a corrupt one drops only the placement', () => {
		const draft = { point: { x_pct: 1, y_pct: 2 }, anchor: '#deck-a', text: 'hi', page: '/', placement };
		const ok = restore.restoreParkedPinDraft(JSON.stringify(draft), '/', { width: 10, height: 10 });
		assert.deepEqual(ok.draft.placement, placement);
		const bad = restore.restoreParkedPinDraft(
			JSON.stringify({ ...draft, placement: { nearby_anchors: 'nope' } }),
			'/',
			{ width: 10, height: 10 }
		);
		assert.equal(bad.draft.text, 'hi', 'a corrupt placement lost the typed text');
		assert.equal(bad.draft.placement, null);
		const legacy = restore.restoreParkedPinDraft(JSON.stringify({ ...draft, placement: undefined }), '/', {
			width: 10,
			height: 10
		});
		assert.equal(legacy.draft.placement, null);
	});
});

describe('findUniqueBySelector', () => {
	const root = (count) => ({ querySelectorAll: () => Array.from({ length: count }, (_, i) => ({ i })) });

	it('resolves only exactly one match, and refuses selector shapes it did not produce', () => {
		assert.deepEqual(pos.findUniqueBySelector('#deck-a', root(1)), { i: 0 });
		assert.equal(pos.findUniqueBySelector('#deck-a', root(2)), null, 'an ambiguous selector resolved');
		assert.equal(pos.findUniqueBySelector('#deck-a', root(0)), null);
		assert.equal(pos.findUniqueBySelector('div > span', root(1)), null, 'an arbitrary CSS selector was run');
	});
});
