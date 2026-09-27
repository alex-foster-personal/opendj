import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

/** Baseline before pin 8cd32a28c36d closeout (issue #4088). */
const BASELINE_STEM_LABEL_MARGIN_TOP_PX = 2;
const BASELINE_FADER_MIN_HEIGHT_PX = 64;

test('pin 8cd32a28c36d STEM row gains separation from fader column budget', async () => {
	const src = await readFile('src/lib/components/rb/mixer/ChannelStrip.svelte', 'utf8');
	assert.match(src, /pin 8cd32a28c36d/);

	const stemRule = src.match(/\.stem-label\s*\{([^}]*)\}/)?.[1] ?? '';
	const stemMargin = Number(stemRule.match(/margin-top:\s*(\d+)px/)?.[1]);
	assert.ok(stemMargin > BASELINE_STEM_LABEL_MARGIN_TOP_PX);

	const faderRule = src.match(/\.fader-slot\s*\{([^}]*)\}/)?.[1] ?? '';
	const faderMin = Number(faderRule.match(/min-height:\s*(\d+)px/)?.[1]);
	assert.ok(faderMin < BASELINE_FADER_MIN_HEIGHT_PX);
});
