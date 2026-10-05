import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const TOPBAR = 'src/lib/components/rb/TopBar.svelte';
const PITCH_FADER = 'src/lib/components/rb/deck/PitchFader.svelte';

test('IOPIN-06 master and pitch surfaces render their observed pickup positions', async () => {
	// [if] hardware was observed [then] master and pitch render its ghost, [else stop].
	const [topbar, pitchFader] = await Promise.all([
		readFile(TOPBAR, 'utf8'),
		readFile(PITCH_FADER, 'utf8')
	]);
	assert.match(topbar, /midiTakeoverGhost\('mixer:global:master'\)/);
	assert.match(topbar, /data-takeover-ghost=\{masterTakeoverGhost/);
	assert.match(topbar, /class="master-takeover-ghost"/);
	assert.match(pitchFader, /midiTakeoverGhost\(`deck:\$\{deck\.deck_id\}:pitch`\)/);
	assert.match(pitchFader, /data-takeover-ghost=\{takeoverGhost/);
	assert.match(pitchFader, /class="rb-fader-ghost"/);
});
