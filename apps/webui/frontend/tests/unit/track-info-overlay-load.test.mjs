import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// requirement: TOPVIEW-05
// The overlay loads once per track. Its loader reads and writes the
// grid-provenance cache and its own `load` state synchronously, so the effect
// that starts it must depend on the deck's track id alone; otherwise the
// cache settling re-runs it (a second GET, and a flash back to loading).

const SRC = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TrackInfoOverlay.svelte', import.meta.url)),
	'utf8'
);

test('the load effect tracks only stableId and runs the loader untracked', () => {
	assert.match(SRC, /import \{ untrack \} from 'svelte';/);
	const effects = SRC.match(/\$effect\(\(\) => \{[\s\S]*?\n\t\}\);/g) ?? [];
	const loader = effects.filter((e) => e.includes('_load('));
	assert.equal(loader.length, 1, 'exactly one effect starts the loader');
	assert.match(loader[0], /const id = stableId;\s*untrack\(\(\) => void _load\(id\)\);/);
	// Control: the loader really does touch the reactive state the effect must not track.
	assert.match(SRC, /async function _load[\s\S]*?load = \{ state: 'loading' \};[\s\S]*?if \(!isUsbTrackId\(id\)\) requestGridProvenance\(id\);/);
});
