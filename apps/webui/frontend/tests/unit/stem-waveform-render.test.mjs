import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let render;

before(async () => {
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
});

function recordingCtx(width, height) {
	const fills = [];
	return {
		fills,
		ctx: {
			fillStyle: '#000',
			clearRect() {},
			fillRect(x, y, w, h) {
				fills.push({ x, y, w, h, fillStyle: this.fillStyle });
			}
		},
		width,
		height
	};
}

test('drawStemWaveRow paints non-empty bars from a fixture envelope', () => {
	const envelope = Array.from({ length: 100 }, (_, i) => (i === 50 ? 1 : 0));
	const rec = recordingCtx(100, 12);
	render.drawStemWaveRow(rec.ctx, {
		envelope,
		scrollPx: 50,
		width: rec.width,
		height: rec.height,
		color: '#4fb2ff'
	});
	assert.ok(rec.fills.length > 0, 'fixture envelope must paint at least one bar');
	assert.ok(
		rec.fills.some((f) => f.fillStyle === '#4fb2ff'),
		'stem row must use the supplied stem colour'
	);
});
