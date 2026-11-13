import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// STRUCT-9659 - the library preview strip names the section under the cursor.
// [if] the cursor sits over a section on a strip with sections on
// [then] the helper returns that section's label, edges resolve start-inclusive
// and end-exclusive, the label is clamped inside the strip, and no spans, no
// hover or a gap return null, [else stop].

let hover;

before(async () => {
	hover = await loadTypeScriptModule('src/lib/components/rb/browser/preview-section-hover.ts');
});

const SPANS = [
	{ start_s: 0, end_s: 30, label: 'Intro' },
	{ start_s: 30, end_s: 60, label: 'Breakdown' },
	{ start_s: 70, end_s: 100, label: 'Drop' }
];

test('sectionIndexAt resolves section edges start-inclusive, end-exclusive', () => {
	// Every section edge maps to the section that starts there.
	assert.equal(hover.sectionIndexAt(SPANS, 0), 0);
	assert.equal(hover.sectionIndexAt(SPANS, 29.999), 0);
	assert.equal(hover.sectionIndexAt(SPANS, 30), 1);
	assert.equal(hover.sectionIndexAt(SPANS, 59.999), 1);
	assert.equal(hover.sectionIndexAt(SPANS, 60), -1);
	assert.equal(hover.sectionIndexAt(SPANS, 70), 2);
	assert.equal(hover.sectionIndexAt(SPANS, 100), -1);
	assert.equal(hover.sectionIndexAt(SPANS, -1), -1);
	assert.equal(hover.sectionIndexAt(SPANS, Number.NaN), -1);
	assert.equal(hover.sectionIndexAt([], 10), -1);
});

test('previewSectionHover names the section under the cursor', () => {
	// 100 px strip over a 100 s track: one pixel is one second.
	assert.deepEqual(hover.previewSectionHover(SPANS, 45, 100, 100, 20), {
		label: 'Breakdown',
		leftPx: 35
	});
	assert.equal(hover.previewSectionHover(SPANS, 30, 100, 100, 20)?.label, 'Breakdown');
	assert.equal(hover.previewSectionHover(SPANS, 100, 100, 100, 20)?.label, 'Drop');
});

test('previewSectionHover clamps the label inside the strip', () => {
	// A label near either edge never spills past the strip.
	assert.equal(hover.previewSectionHover(SPANS, 2, 100, 100, 40)?.leftPx, 0);
	assert.equal(hover.previewSectionHover(SPANS, 98, 100, 100, 40)?.leftPx, 60);
	assert.equal(hover.previewSectionHover(SPANS, -5, 100, 100, 40)?.label, 'Intro');
	assert.equal(hover.clampLabelLeft(10, 200, 100), 0);
});

test('previewSectionHover hides with no hover, no spans, no duration or a gap', () => {
	// Pointer leave and show_sections off both reach the helper as null.
	assert.equal(hover.previewSectionHover(SPANS, null, 100, 100, 20), null);
	assert.equal(hover.previewSectionHover(null, 50, 100, 100, 20), null);
	assert.equal(hover.previewSectionHover([], 50, 100, 100, 20), null);
	assert.equal(hover.previewSectionHover(SPANS, 50, 100, null, 20), null);
	assert.equal(hover.previewSectionHover(SPANS, 50, 100, 0, 20), null);
	assert.equal(hover.previewSectionHover(SPANS, 65, 100, 100, 20), null);
});
