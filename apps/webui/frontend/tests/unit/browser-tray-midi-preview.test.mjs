/**
 * requirement: CHROME-07
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const panel = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)),
	'utf8'
);

test('bottom tray renders MIDI 20px left of Preview', () => {
	assert.match(panel, /class="tray-midi"/);
	assert.match(panel, /class="tray-preview"/);
	assert.match(panel, /margin-right:\s*20px/);
	const midiAt = panel.indexOf('class="tray-midi"');
	const previewAt = panel.indexOf('class="tray-preview"');
	assert.ok(midiAt >= 0 && previewAt > midiAt);
});

test('tray MIDI opens the I/O view', () => {
	assert.match(panel, /openIoView\(\)/);
});

test('tray MIDI acquires audio devices in its click before opening I/O, like the mixer I/O button', () => {
	// [if] the tray entry opens I/O [then] it dispatches headphone_output_acquire
	// inside the same click first (the chooser needs the user gesture, and
	// device labels stay locked without it), [else stop] (Codex P2 4132045422).
	const at = panel.indexOf('class="tray-midi"');
	const button = panel.slice(at, panel.indexOf('</button>', at));
	assert.match(
		button,
		/onclick=\{\(\) => \{[\s\S]*?runPerformanceCommandFromUi\(\{ type: 'headphone_output_acquire' \}\);\s*openIoView\(\);\s*\}\}/
	);
	// Control: the mixer's I/O button dispatches the same command.
	const mixer = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/Mixer.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(mixer, /runPerformanceCommandFromUi\(\{ type: 'headphone_output_acquire' \}\)/);
});

test('openIoView pins the headphone I/O menu without auto-opening MIDI drawer', () => {
	const ioSurface = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/io-surface.svelte.ts', import.meta.url)),
		'utf8'
	);
	const cluster = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url)),
		'utf8'
	);
	assert.doesNotMatch(ioSurface, /toggleMidiPanel/);
	// The preview's persistent I/O panel (IOPIN) is what ioSurface opens, so a
	// tray press and the cluster's own I/O button show the same panel.
	assert.match(cluster, /\{#if ioSurface\.open\}[\s\S]*?class="hp-panel"/);
	assert.match(cluster, /function closeIo\(\): void \{[\s\S]*?closeIoView\(\);/);
});

test('the I/O view MIDI entry keeps the status the top-bar MIDI label used to show', () => {
	const cluster = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/mixer/HeadphoneCluster.svelte', import.meta.url)),
		'utf8'
	);
	assert.match(cluster, /midiLabelStatus\(midiState\.permission, midiUi\.requestPending, midiMappedCount > 0, midiOn\)/);
	// An explicit off must read the persisted choice, not a constant (Codex P2, PR #3896).
	assert.match(cluster, /const midiOn = \$derived\(midiEnabledPersisted\(\)\)/);
	assert.match(cluster, /midiLabelTitle\([^)]*, midiOn\)/);
	// Both entries carry the derived status as their class (st-grey, st-green,
	// st-amber, st-red), and each of the four has its own rule.
	assert.equal((cluster.match(/class="hp-btn midi-btn[^"]* st-\{midiStatus\}"/g) ?? []).length, 2);
	for (const status of ['grey', 'green', 'amber', 'red']) {
		assert.match(cluster, new RegExp(`\\.midi-btn\\.st-${status} \\{`));
	}
	assert.match(cluster, /bullets=\{\[midiTitle, /, 'the MIDI explainer must say why the color is what it is');
	assert.match(cluster, /<MidiStatusGlyph glyph=\{midiGlyph\} \/>/);
});

test('settings config exposes MIDI controller enablement', async () => {
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const entry = catalog.SETTINGS_CATALOG.find((def) => def.id === 'rb.midi_enabled');
	assert.ok(entry, 'rb.midi_enabled must exist in settings catalog');
	assert.equal(entry.implemented, true);
});
