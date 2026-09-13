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
