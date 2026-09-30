// requirement: UX-FLOAT-01
import assert from 'node:assert/strict';
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const VIEWPORT = { width: 1280, height: 720 };
const FLOAT_SIZE = { width: 240, height: 180 };
const TRIGGER_SIZE = { width: 24, height: 24 };
const MARGIN = 8;

let clamp;

before(async () => {
	clamp = await loadTypeScriptModule('src/lib/ui/clamp-to-viewport.ts');
});

function assertInsideInset(box, size = FLOAT_SIZE) {
	assert.ok(box.x >= MARGIN, `x ${box.x} < ${MARGIN}`);
	assert.ok(box.y >= MARGIN, `y ${box.y} < ${MARGIN}`);
	assert.ok(box.x + size.width <= VIEWPORT.width - MARGIN, 'right edge overflow');
	assert.ok(box.y + size.height <= VIEWPORT.height - MARGIN, 'bottom edge overflow');
}

const EDGE_CASES = [
	['top edge', 628, 0],
	['bottom edge', 628, 696],
	['left edge', 0, 348],
	['right edge', 1256, 348],
	['top-left', 0, 0],
	['top-right', 1256, 0],
	['bottom-left', 0, 696],
	['bottom-right', 1256, 696]
];

for (const [name, left, top] of EDGE_CASES) {
	test(`placeFloating keeps a hover tile inside the viewport from the ${name}`, () => {
		const box = clamp.placeFloating({
			trigger: { left, top, width: TRIGGER_SIZE.width, height: TRIGGER_SIZE.height },
			size: FLOAT_SIZE,
			viewport: VIEWPORT,
			preferred: 'below'
		});
		assertInsideInset(box);
	});
}

test('clampToViewport pulls an off-screen bottom-right point back inside', () => {
	const box = clamp.clampToViewport(2000, 2000, FLOAT_SIZE, VIEWPORT);
	assert.deepEqual(box, {
		x: VIEWPORT.width - FLOAT_SIZE.width - MARGIN,
		y: VIEWPORT.height - FLOAT_SIZE.height - MARGIN
	});
});

test('clampToViewport pins an oversized box to the top-left margin', () => {
	const box = clamp.clampToViewport(100, 100, { width: 1400, height: 800 }, VIEWPORT);
	assert.deepEqual(box, { x: MARGIN, y: MARGIN });
});

// PR #4094: a floating box must not be clamped back over its own trigger when
// the opposite side has room. The flip used to be vetoed by an overflow on the
// CROSS axis (which clamping fixes anyway), so a corner trigger kept a side
// that did not fit and was clamped up over itself, where the tile swallowed
// the trigger's clicks (the feedback dock's comment-pin button, bottom right).
function overlaps(box, size, rect) {
	return (
		box.x < rect.left + rect.width &&
		box.x + size.width > rect.left &&
		box.y < rect.top + rect.height &&
		box.y + size.height > rect.top
	);
}

for (const preferred of ['below', 'above', 'right', 'left']) {
	for (const [name, left, top] of EDGE_CASES) {
		test(`placeFloating ${preferred} from the ${name} stays inside and off its trigger`, () => {
			const trigger = { left, top, width: TRIGGER_SIZE.width, height: TRIGGER_SIZE.height };
			const box = clamp.placeFloating({ trigger, size: FLOAT_SIZE, viewport: VIEWPORT, preferred });
			assertInsideInset(box);
			assert.ok(!overlaps(box, FLOAT_SIZE, trigger), `${JSON.stringify(box)} covers its trigger`);
		});
	}
}

test('placeFloating: an explainer centered on a bottom-right dock button flips above it', () => {
	// ControlExplainer passes a trigger centered on the button and as wide as
	// the popover, so it overflows the right edge whichever side it takes.
	const button = { left: 1236, top: 676, width: 32, height: 32 };
	const size = { width: 240, height: 120 };
	const trigger = {
		left: button.left + button.width / 2 - size.width / 2,
		top: button.top,
		width: size.width,
		height: button.height
	};
	const box = clamp.placeFloating({ trigger, size, viewport: VIEWPORT, preferred: 'below', gap: 6 });
	assertInsideInset(box, size);
	assert.equal(box.y, button.top - size.height - 6, 'flipped above the button');
	assert.ok(!overlaps(box, size, button), `${JSON.stringify(box)} covers the button`);
});

test('placeFloating keeps a preferred side that fits even when the cross axis overflows', () => {
	// Control for the overshoot: flipping on ANY overflow would move this tile
	// above a trigger it already fits below.
	const trigger = { left: 1256, top: 348, width: TRIGGER_SIZE.width, height: TRIGGER_SIZE.height };
	const below = clamp.placeFloating({ trigger, size: FLOAT_SIZE, viewport: VIEWPORT, preferred: 'below' });
	assert.equal(below.y, 348 + 24 + 4, 'stays below');
	const right = clamp.placeFloating({
		trigger: { left: 628, top: 0, width: 24, height: 24 },
		size: FLOAT_SIZE,
		viewport: VIEWPORT,
		preferred: 'right'
	});
	assert.equal(right.x, 628 + 24 + 4, 'stays right');
});

test('placeFloating with no room on either side of the main axis keeps the preferred side, clamped', () => {
	const tiny = { width: 300, height: 200 };
	const box = clamp.placeFloating({
		trigger: { left: 138, top: 88, width: 24, height: 24 },
		size: FLOAT_SIZE,
		viewport: tiny,
		preferred: 'below'
	});
	assert.deepEqual(box, { x: 52, y: 12 });
});
