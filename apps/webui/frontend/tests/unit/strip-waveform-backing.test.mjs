// DECKUX-8337: the deck strip draws at its real device size, not a fixed 400x40
// stretched by CSS (pixelated on an ultrawide, the maintainer Tue 3 Nov 2026).
// - if a 900 px strip at 2x still draws 400 wide then it upscales 4.5x -> broken
// - if an unmeasured strip (0x0) draws 0 wide then the first paint is blank -> broken
// - if a huge strip is not capped then one resize paints a giant canvas -> broken
// -Claude
import assert from 'node:assert/strict';
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let stripBackingSize, STRIP_MIN_BACKING, STRIP_MAX_BACKING_PX;
before(async () => {
	({ stripBackingSize, STRIP_MIN_BACKING, STRIP_MAX_BACKING_PX } = await loadTypeScriptModule(
		'src/lib/components/rb/deck/strip-waveform-render.ts'
	));
});

test('a 900x24 CSS strip at 2x draws at 1800x48 device px', () => {
	assert.deepEqual(stripBackingSize(900, 24, 2), { widthPx: 1800, heightPx: 48 });
});

test('an unmeasured strip falls back to the old 400x40 buffer', () => {
	assert.deepEqual(stripBackingSize(0, 0, 2), { widthPx: STRIP_MIN_BACKING.widthPx, heightPx: STRIP_MIN_BACKING.heightPx });
});

test('a very wide strip is capped per side', () => {
	assert.equal(stripBackingSize(5120, 30, 2).widthPx, STRIP_MAX_BACKING_PX);
});

test('a non-positive device pixel ratio is refused', () => {
	assert.throws(() => stripBackingSize(900, 24, 0), /devicePixelRatio/);
});
