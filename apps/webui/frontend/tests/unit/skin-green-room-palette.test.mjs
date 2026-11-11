// requirement: SKINLAYOUT-8869d
// [if] Green Room overrides an explicit Legacy or Mono waveform palette on the main rows [then] the rows and strips disagree, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const THEME = readFileSync('src/lib/rb/theme.css', 'utf8');

function block(selector) {
	const start = THEME.indexOf(`${selector} {`);
	assert.ok(start !== -1, `no \`${selector} {}\` block in theme.css`);
	const body = THEME.slice(start, THEME.indexOf('}', start));
	return Object.fromEntries([...body.matchAll(/(--rb-wave-[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

test('if Green Room does not repeat the dark Legacy and Mono palettes after its skin block then explicit palettes are ignored', () => {
	const skinAt = THEME.indexOf("html[data-skin='green-room'] .perf-root {");
	for (const palette of ['legacy', 'mono', 'blue']) {
		const selector = `html[data-skin='green-room'][data-wave-palette='${palette}'] .perf-root`;
		assert.ok(THEME.indexOf(selector) > skinAt, `${selector} must follow the skin block`);
		assert.deepEqual(block(selector), block(`html[data-wave-palette='${palette}'] .perf-root`));
	}
});
