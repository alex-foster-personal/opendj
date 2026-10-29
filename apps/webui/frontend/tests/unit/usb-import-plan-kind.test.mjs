// requirement: USBIMP-01
// [if] the DJ opens a djay or plain music stick [then] the stick is imported and analyzed and the panel says it cannot be ranked [⛔️ if the plan reads export.pdb on a stick that has none, or presents a ranked plan for it].
//
// Codex P2 (BLOCKING) on #6223: the plan's only source is the stick's rekordbox
// export.pdb (GET /usb/{id}/library), so a non-RBX stick must not reach it.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let open;

before(async () => {
	open = await loadTypeScriptModule('src/lib/rb/usb-import-open.ts');
});

test('djay, plain music and unknown sticks get no ranked plan, with copy that says so', () => {
	for (const kind of ['djay', 'music', 'unknown', undefined]) {
		const why = open.usbPlanRefusal({ id: 'vol:ABC', kind });
		assert.match(why ?? '', /no rekordbox export, so its tracks cannot be ranked/, String(kind));
		assert.match(why ?? '', /still imported and analyzed/, String(kind));
	}
});

test('control: a rekordbox stick with an id is ranked', () => {
	assert.equal(open.usbPlanRefusal({ id: 'vol:ABC', kind: 'rekordbox' }), null);
});

test('control: a rekordbox volume with no id is still refused for its own reason', () => {
	assert.match(open.usbPlanRefusal({ id: 'mount:/Volumes/X', kind: 'rekordbox' }) ?? '', /has no id/);
});

test('the refusal is checked before the stick library is read', () => {
	const src = readFileSync(new URL('../../src/lib/rb/usb-import-open.ts', import.meta.url), 'utf8');
	const body = src.slice(src.indexOf('export async function openUsbImportPlan'));
	const refusalAt = body.indexOf('usbPlanRefusal(vol)');
	const readAt = body.indexOf('revalidateUsbLibrary(');
	assert.ok(refusalAt > 0 && readAt > refusalAt, 'the kind gate must run before revalidateUsbLibrary');
});
