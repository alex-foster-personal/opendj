/**
 * CMDK pick command race (Codex BLOCKING, #6692): a seek or tempo sent to the deck while the pick's
 * guard waits must refuse the pick, not be adopted as the pick's baseline by pickLoad.begin.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
const guard = readFileSync(new URL('../../src/lib/rb/cmdk-deck-picker.ts', import.meta.url), 'utf8');

test('CMDK-pick-command-race: the pick guard snapshots the command count with the load count', () => {
	assert.match(guard, /\tcommands\(\): number;/);
	assert.match(guard, /seen\.loads = ports\.loads\(\);\s*seen\.commands = ports\.commands\(\);/);
});

test('CMDK-pick-command-race: the bar refuses a pick whose deck took a command after the verdict', () => {
	assert.match(bar, /const seen = \{ loads: -1, commands: -1 \};/);
	assert.match(bar, /commands: \(\) => deckCommands\(id\),/);
	assert.match(bar, /if \(deckCommands\(id\) !== seen\.commands\) throw new Error/);
});
