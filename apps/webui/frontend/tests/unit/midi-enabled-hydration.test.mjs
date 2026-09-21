// A disk-hydrated midi_enabled=true must re-run the auto-enable, not wait for
// a second reload (Codex P2 on PR #3726). The leaf module carries the hook;
// midi-ui-state registers the re-run at import.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { readFrontendSource } from './engine-source.mjs';

test('hydrating midi_enabled=true fires the registered listener with true, and a body without the key fires nothing', async () => {
	const choice = await loadTypeScriptModule('src/lib/components/rb/midi/midi-enabled-choice.ts');
	const seen = [];
	choice.onMidiEnabledHydrated((enabled) => seen.push(enabled));
	choice.hydrateMidiEnabledFromDisk({});
	assert.deepEqual(seen, [], 'a body without midi_enabled must not fire');
	choice.hydrateMidiEnabledFromDisk({ midi_enabled: true });
	choice.hydrateMidiEnabledFromDisk({ midi_enabled: false });
	assert.deepEqual(seen, [true, false]);
});

test('midi-ui-state registers the auto-enable re-run on hydration', () => {
	const src = readFrontendSource('src/lib/components/rb/midi/midi-ui-state.svelte.ts');
	assert.match(
		src,
		/onMidiEnabledHydrated\(\(enabled\) => \{\s*if \(enabled\) void maybeAutoEnableMidi\(\);/,
		'the hydrated choice must re-run maybeAutoEnableMidi, or a disk-only opt-in needs a second reload'
	);
	const leaf = readFrontendSource('src/lib/components/rb/midi/midi-enabled-choice.ts');
	assert.doesNotMatch(
		leaf,
		/from '[^']*(midi-ui-state|action-glue|performance-ipc)[^']*'/,
		'the leaf must stay import-cycle free (its docstring may name those modules; an import may not)'
	);
});
