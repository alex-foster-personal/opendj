// requirement: CUEOUT-23
// Perf (perf-lab Tue 13 Oct 2026): an idle page must not repaint for a pulse.
// A keyframe on box-shadow or background repaints its element every frame; one
// on opacity of a pseudo-element runs on the compositor.
// The deck-hover library row no longer pulses at all: main replaced it with a
// static left-edge tick (DECKUX-5917), so it has no keyframe to check here.
// [if] a pulse keyframe animates a paint property [then] this fails and names it
// [if] the static glow the alert pulse rests on is removed [then] this fails (the alert stays visible
//   between pulses and under reduced motion)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const source = (relativePath) =>
	readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');

const PULSES = [
	['../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', 'io-alert-pulse'],
	['../../src/lib/components/rb/browser/IconRail.svelte', 'rec-waiting']
];

function keyframes(text, name) {
	const match = text.match(new RegExp(`@keyframes ${name} \\{([\\s\\S]*?)\\n\\t\\}`));
	return match === null ? null : match[1];
}

test('pulse keyframes animate opacity only', () => {
	for (const [file, name] of PULSES) {
		const body = keyframes(source(file), name);
		assert.ok(body !== null, `@keyframes ${name} must exist in ${file}`);
		assert.match(body, /opacity:/, `${name} must animate opacity`);
		assert.doesNotMatch(
			body,
			/box-shadow|background|outline|filter/,
			`${name} animates a paint property: it repaints every frame`
		);
	}
});

test('control: the keyframe reader sees a paint-property keyframe', () => {
	const planted = '\t@keyframes bad {\n\t\t50% {\n\t\t\tbox-shadow: 0 0 9px red;\n\t\t}\n\t}';
	assert.match(keyframes(planted, 'bad'), /box-shadow/);
	assert.equal(keyframes(planted, 'missing'), null);
});

test('the I/O alert pulse rests on a static, visible glow', () => {
	const cluster = source('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte');
	const button = cluster.match(/\t\.hp-btn-io\.io-alert \{([^}]*)\}/);
	assert.ok(button);
	assert.match(button[1], /box-shadow: 0 0 \d+px/, 'the button keeps its own static red glow');
	assert.match(button[1], /border-color: var\(--rb-red/);
	const layer = cluster.match(/\t\.hp-btn-io\.io-alert::after \{([^}]*)\}/);
	assert.ok(layer);
	assert.match(layer[1], /animation: io-alert-pulse /);
	assert.doesNotMatch(
		layer[1],
		/forwards|both/,
		'a fill mode would freeze the glow layer at an arbitrary frame'
	);
});
