// requirement: USBPLAY-02
/**
 * Play from USB permission states in the stick list (usb-tracker.svelte.ts).
 *
 * The daemon adds a per-volume `access` field to GET /api/v1/usb/volumes:
 * ok | pending | denied | unknown. It answers a USB volume whose root did not
 * list as role usb_stick, is_music true (usb_classify.classify_role with
 * root_listed=False, usb_volumes._to_out unreadable_stick), so a blocked
 * stick stays listed with its state; a blocked Fixed drive or disk image
 * keeps its own role and is_music false. Fixtures here use that exact wire.
 * These tests drive the tracker's real poll against a REAL local HTTP server;
 * nothing replaces fetch.
 *
 * Regression lines:
 * - [if] a denied stick is folded away as non-music [then] it silently
 *   disappears instead of saying Open DJ cannot read it
 * - [if] a pending stick is not listed [then] the permission prompt reads as
 *   a missing stick
 * - [if] a blocked disk image or Thunderbolt drive is listed [then] it takes
 *   the first-seen prompt and an Import that would queue the whole drive
 * - [if] an automatic is_music=false outlives the daemon's verdict [then] a
 *   stick an older daemon saw while permission was refused stays hidden for
 *   good once permission is granted
 * - [if] the daemon's verdict overrides the user's "Not music" answer [then]
 *   a stick the DJ put away keeps coming back
 * - [if] a readable non-stick drive stops folding [then] the access handling
 *   swallowed the ordinary role verdict
 * - [if] a daemon without the field gets an invented access value [then] the
 *   UI claims a permission state nobody measured
 */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { after, before, describe, it, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let usb;
let usbImport;
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
	usbImport = await loadTypeScriptModule('src/lib/rb/usb-import.ts');
});

after(async () => {
	server.closeAllConnections();
	await new Promise((resolve) => server.close(resolve));
});

/** A readable Fixed drive with no DJ export: the daemon's plain non-stick. */
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
		access: 'ok',
		...extra
	};
}

/** What the daemon sends for a USB stick whose root did not list. */
function blockedStick(id, access) {
	return apiVolume(id, { role: 'usb_stick', kind: 'unknown', is_music: true, access });
}

/** What the daemon sends for a readable rekordbox stick. */
function readableStick(id) {
	return apiVolume(id, { role: 'usb_stick', kind: 'rekordbox', is_music: true, access: 'ok' });
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

	it('folds on the stored verdict alone, whatever the access state', () => {
		// The daemon already answers a blocked stick as music; access is never
		// a second way into the list (that is how blocked drives leaked in).
		const base = { id: 'vol:X', name: 'X', first_seen: 1, last_seen: 2, present: true, is_music: false };
		assert.equal(usb.isActiveUsbRow({ ...base, access: 'denied' }), false);
		assert.equal(usb.isFoldedUsbRow({ ...base, access: 'denied' }), true);
		assert.equal(usb.isActiveUsbRow({ ...base, is_music: true, access: 'denied' }), true);
		// Forgetting still wins over a blocked state.
		assert.equal(usb.isActiveUsbRow({ ...base, is_music: true, access: 'pending', forgotten: true }), false);
	});

	it('offers no Import while access is blocked', () => {
		const vol = {
			id: 'vol:X',
			name: 'X',
			mount_path: '/Volumes/X',
			first_seen: 1,
			last_seen: 2,
			present: true,
			is_music: true
		};
		assert.equal(usbImport.canImportUsbVolume({ ...vol, access: 'ok' }), true, 'control');
		assert.equal(usbImport.canImportUsbVolume({ ...vol, access: 'pending' }), false);
		assert.equal(usbImport.canImportUsbVolume({ ...vol, access: 'denied' }), false);
	});
});

test('a denied stick stays listed, and stays a music stick once readable', async () => {
	await poll([blockedStick('vol:DENIED', 'denied')]);
	const row = known('vol:DENIED');
	assert.equal(row.access, 'denied');
	assert.equal(row.is_music, true);
	assert.ok(listed('vol:DENIED'), 'a denied stick must stay in the list');

	// Permission granted: the daemon now sees PIONEER/.
	await poll([readableStick('vol:DENIED')]);
	assert.equal(known('vol:DENIED').access, 'ok');
	assert.equal(known('vol:DENIED').is_music, true);
	assert.ok(listed('vol:DENIED'));
});

test('a stick whose permission prompt is open is listed as pending', async () => {
	await poll([blockedStick('vol:PENDING', 'pending')]);
	assert.equal(known('vol:PENDING').access, 'pending');
	assert.ok(listed('vol:PENDING'));
});

for (const [label, id, extra] of [
	['a disk image', 'vol:IMAGE', { role: 'disk_image', protocol: 'Disk Image' }],
	['a Thunderbolt drive', 'vol:TBOLT', { role: 'mounted_drive', protocol: 'Thunderbolt' }]
]) {
	for (const access of ['pending', 'denied']) {
		test(`${label} with access ${access} stays folded, with no prompt and no Import`, async () => {
			const volId = `${id}-${access}`;
			usb.usbTracker.promptId = null;
			await poll([apiVolume(volId, { ...extra, access })]);
			const row = known(volId);
			assert.equal(listed(volId), false, 'a blocked non-stick must not be promoted to a stick');
			assert.equal(row.is_music, false);
			assert.equal(row.needs_prompt, false);
			assert.notEqual(usb.usbTracker.promptId, volId, 'no first-seen prompt for a non-stick');
			assert.equal(usbImport.canImportUsbVolume(row), false);
		});
	}
}

test('a stick an older daemon stored as non-music comes back once the daemon says music', async () => {
	// Before USBPLAY-02 the daemon reported a stick with refused permission as
	// a mounted drive, so the tracker stored is_music=false for it.
	await poll([apiVolume('vol:LEGACY', { access: undefined })]);
	assert.equal(known('vol:LEGACY').is_music, false);
	assert.equal(listed('vol:LEGACY'), false);
	// Today's daemon, still denied: a usb_stick that is music.
	await poll([blockedStick('vol:LEGACY', 'denied')]);
	assert.ok(listed('vol:LEGACY'), 'the stored automatic verdict must not outlive the daemon');
	// Permission granted.
	await poll([readableStick('vol:LEGACY')]);
	assert.equal(known('vol:LEGACY').is_music, true);
	assert.ok(listed('vol:LEGACY'));
});

test('a stick whose root failed to list for another reason folds only while the daemon says so', async () => {
	await poll([apiVolume('vol:FLAKY', { role: 'usb_stick', access: 'unknown' })]);
	assert.equal(listed('vol:FLAKY'), false);
	await poll([readableStick('vol:FLAKY')]);
	assert.ok(listed('vol:FLAKY'), 'an access=unknown verdict must not stick');
});

test("the user's Not music answer outlives the daemon, and Music does not override a Fixed drive", async () => {
	await poll([readableStick('vol:PUTAWAY')]);
	assert.ok(listed('vol:PUTAWAY'));
	usb.applyFirstSeen('vol:PUTAWAY', { yours: false, is_music: false });
	assert.equal(listed('vol:PUTAWAY'), false);
	await poll([readableStick('vol:PUTAWAY')]);
	assert.equal(listed('vol:PUTAWAY'), false, 'the user answered Not music');

	// Overshoot control: a "Music" answer is not a way to list a Fixed drive
	// the daemon calls a mounted drive (USBPLAY-01).
	await poll([readableStick('vol:SAIDMUSIC')]);
	usb.applyFirstSeen('vol:SAIDMUSIC', { yours: true, is_music: true });
	await poll([apiVolume('vol:SAIDMUSIC')]);
	assert.equal(listed('vol:SAIDMUSIC'), false);
});

test('a readable drive that is not a stick still folds away (control)', async () => {
	await poll([apiVolume('vol:BACKUP', { access: 'ok' })]);
	assert.equal(known('vol:BACKUP').is_music, false);
	assert.equal(listed('vol:BACKUP'), false);
});

test('a daemon that does not report access gets no invented value', async () => {
	await poll([apiVolume('vol:OLD', { role: 'usb_stick', is_music: true, access: undefined })]);
	assert.equal('access' in known('vol:OLD'), false);
	assert.ok(listed('vol:OLD'));
});
