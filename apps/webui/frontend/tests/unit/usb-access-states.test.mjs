// requirement: USBPLAY-02
/**
 * Play from USB permission states in the stick list (usb-tracker.svelte.ts).
 *
 * The daemon adds a per-volume `access` field to GET /api/v1/usb/volumes:
 * ok | pending | denied | unknown. A drive macOS will not let Open DJ read
 * looks like a plain disk (no PIONEER/ visible), so its role is no verdict.
 * These tests drive the tracker's real poll against a REAL local HTTP server;
 * nothing replaces fetch.
 *
 * Regression lines:
 * - [if] a denied stick is folded away as non-music [then] it silently
 *   disappears instead of saying Open DJ cannot read it
 * - [if] is_music=false is persisted while access is blocked [then] the stick
 *   stays hidden for good after permission is granted
 * - [if] a pending stick is not listed [then] the permission prompt reads as
 *   a missing stick
 * - [if] a readable non-stick drive stops folding [then] the access guard
 *   swallowed the ordinary role verdict
 * - [if] a daemon without the field gets an invented access value [then] the
 *   UI claims a permission state nobody measured
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, describe, it, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let usb;
let server;
let volumes = [];

before(async () => {
	server = createServer((req, res) => {
		if (req.url !== '/api/v1/usb/volumes') {
			res.writeHead(404, { 'content-type': 'application/json' });
			res.end(JSON.stringify({ detail: { code: 'NOT_FOUND', message: req.url } }));
			return;
		}
		res.writeHead(200, { 'content-type': 'application/json' });
		res.end(JSON.stringify({ volumes, scanned_at: 0, watching: false }));
	});
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	usb = await loadTypeScriptModule('src/lib/rb/usb-tracker.svelte.ts', {
		viteApiBase: `http://127.0.0.1:${server.address().port}`
	});
});

after(async () => {
	server.closeAllConnections();
	await new Promise((resolve) => server.close(resolve));
});

function apiVolume(id, extra) {
	return {
		id,
		name: `FIXTURE ${id}`,
		mount_path: `/Volumes/FIXTURE ${id}`,
		kind: 'unknown',
		present: true,
		simulated: false,
		is_music: false,
		role: 'mounted_drive',
		...extra
	};
}

async function poll(next) {
	volumes = next;
	await usb.refreshUsbVolumes();
	assert.equal(usb.usbTracker.lastError, null, `poll failed: ${usb.usbTracker.lastError}`);
}

function known(id) {
	const row = usb.usbTracker.volumes.find((v) => v.id === id);
	assert.ok(row, `tracker has no row for ${id}`);
	return row;
}

function listed(id) {
	return usb.presentNonForgotten().some((v) => v.id === id);
}

describe('access helpers', () => {
	it('treats only pending and denied as blocked', () => {
		assert.equal(usb.usbAccessBlocked({ access: 'pending' }), true);
		assert.equal(usb.usbAccessBlocked({ access: 'denied' }), true);
		assert.equal(usb.usbAccessBlocked({ access: 'ok' }), false);
		assert.equal(usb.usbAccessBlocked({ access: 'unknown' }), false);
		assert.equal(usb.usbAccessBlocked({}), false);
	});

	it('keeps a blocked drive listed even when marked non-music, and folds a readable one', () => {
		const base = { id: 'vol:X', name: 'X', first_seen: 1, last_seen: 2, present: true, is_music: false };
		assert.equal(usb.isActiveUsbRow({ ...base, access: 'denied' }), true);
		assert.equal(usb.isFoldedUsbRow({ ...base, access: 'denied' }), false);
		assert.equal(usb.isActiveUsbRow({ ...base, access: 'ok' }), false);
		assert.equal(usb.isFoldedUsbRow({ ...base, access: 'ok' }), true);
		// Forgetting still wins over a blocked state.
		assert.equal(usb.isActiveUsbRow({ ...base, access: 'pending', forgotten: true }), false);
	});
});

test('a denied stick stays listed, and becomes a music stick once readable', async () => {
	await poll([apiVolume('vol:DENIED', { access: 'denied' })]);
	const row = known('vol:DENIED');
	assert.equal(row.access, 'denied');
	assert.equal(row.is_music, undefined, 'no content verdict may be stored while access is blocked');
	assert.ok(listed('vol:DENIED'), 'a denied stick must stay in the list');

	// Still denied on the next poll: the known-row path must not store a
	// verdict either (this is the path that used to persist is_music=false).
	await poll([apiVolume('vol:DENIED', { access: 'denied' })]);
	assert.equal(known('vol:DENIED').is_music, undefined);
	assert.ok(listed('vol:DENIED'));

	// Permission granted: the daemon now sees PIONEER/ and calls it a stick.
	await poll([
		apiVolume('vol:DENIED', { access: 'ok', role: 'usb_stick', kind: 'rekordbox', is_music: true })
	]);
	assert.equal(known('vol:DENIED').access, 'ok');
	assert.equal(known('vol:DENIED').is_music, true);
	assert.ok(listed('vol:DENIED'));
});

test('a stick whose permission prompt is open is listed as pending', async () => {
	await poll([apiVolume('vol:PENDING', { access: 'pending' })]);
	assert.equal(known('vol:PENDING').access, 'pending');
	assert.ok(listed('vol:PENDING'));
});

test('a readable drive that is not a stick still folds away (control)', async () => {
	await poll([apiVolume('vol:BACKUP', { access: 'ok' })]);
	assert.equal(known('vol:BACKUP').is_music, false);
	assert.equal(listed('vol:BACKUP'), false);
});

test('a daemon that does not report access gets no invented value', async () => {
	await poll([apiVolume('vol:OLD', { role: 'usb_stick', is_music: true })]);
	assert.equal('access' in known('vol:OLD'), false);
	assert.ok(listed('vol:OLD'));
});
