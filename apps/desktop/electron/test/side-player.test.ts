// SPOT-6555b/03: the side player's geometry and the Chrome sign-in import.
import * as assert from 'node:assert/strict';
import { createCipheriv, createHash, pbkdf2Sync } from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import {
	LOGIN_SITES,
	chromeKeyFromPassword,
	chromeProfileCookies,
	chromeTimeToUnix,
	decryptChromeValue,
	hostIsLoginSite,
	importConsentText,
	loginSiteWhere,
	readChromeLoginCookies,
	snapshotSqlite,
	storeImportedCookies,
	toImportedCookie,
	unsupportedPlatformReason
} from '../src/chrome-logins';
import {
	MAIN_MIN_W,
	PLAYER_W,
	STRIP_W,
	closedMainWidth,
	isSidePlayerOpenTarget,
	isSidePlayerUrl,
	plainChromeUserAgent,
	sideCollapsedArg,
	sideDockUnavailableReason,
	sideOpenArgs,
	sidePlayerLayout
} from '../src/side-player-layout';
import { FIXTURES, scratchDir } from './helpers';

// ----- geometry -----------------------------------------------------------------
const SCREEN = { x: 0, y: 25, width: 1512, height: 891 };

test('expanding shrinks a full-width Open DJ just enough and docks the player at its edge', () => {
	const main = { x: 0, y: 25, width: 1512, height: 891 };
	const out = sidePlayerLayout({ main, workArea: SCREEN, collapsed: false, shrinkMain: true, restoreWidth: null });
	assert.equal(out.main.width, 1512 - PLAYER_W);
	assert.equal(out.restoreWidth, 1512);
	assert.deepEqual(out.player, { x: 1512 - PLAYER_W, y: 25, width: PLAYER_W, height: 891 });
});

test('with room to spare Open DJ is left alone', () => {
	const main = { x: 100, y: 40, width: 900, height: 700 };
	const out = sidePlayerLayout({ main, workArea: SCREEN, collapsed: false, shrinkMain: true, restoreWidth: null });
	assert.deepEqual(out.main, main);
	assert.equal(out.restoreWidth, null);
	assert.equal(out.player.x, 1000);
});

test('collapsing to the 10px column gives Open DJ its width back', () => {
	const shrunk = sidePlayerLayout({
		main: { x: 0, y: 25, width: 1512, height: 891 },
		workArea: SCREEN,
		collapsed: false,
		shrinkMain: true,
		restoreWidth: null
	});
	const out = sidePlayerLayout({
		main: shrunk.main,
		workArea: SCREEN,
		collapsed: true,
		shrinkMain: true,
		restoreWidth: shrunk.restoreWidth
	});
	assert.equal(STRIP_W, 10);
	assert.equal(out.player.width, 10);
	assert.equal(out.main.width, 1512 - 10);
	assert.equal(out.player.x, 1512 - 10);
	// Collapsed, the original full width stays the target for closing.
	assert.equal(out.restoreWidth, 1512);
	// Re-expanding shrinks again and still remembers the full width.
	const again = sidePlayerLayout({ main: out.main, workArea: SCREEN, collapsed: false, shrinkMain: true, restoreWidth: out.restoreWidth });
	assert.equal(again.main.width, 1512 - PLAYER_W);
	assert.equal(again.restoreWidth, 1512);
});

test('closing gives back the lent width, but never past the display Open DJ is on now', () => {
	// Same display: the full width comes back.
	assert.equal(closedMainWidth({ x: 0, y: 25, width: 1512 - PLAYER_W, height: 891 }, SCREEN, 1512), 1512);
	// Moved to a narrower display while open: clamp at its right edge.
	const narrow = { x: 1512, y: 25, width: 1280, height: 775 };
	assert.equal(closedMainWidth({ x: 1612, y: 25, width: 1000, height: 775 }, narrow, 1512), 1512 + 1280 - 1612);
	// Never narrower than it is now, even when it already overhangs.
	assert.equal(closedMainWidth({ x: 1612, y: 25, width: 1300, height: 775 }, narrow, 1512), 1300);
});

test('collapse/expand cycles never ratchet Open DJ narrower', () => {
	let main = { x: 0, y: 25, width: 1512, height: 891 };
	let restoreWidth: number | null = null;
	for (let i = 0; i < 5; i++) {
		for (const collapsed of [false, true]) {
			const out = sidePlayerLayout({ main, workArea: SCREEN, collapsed, shrinkMain: true, restoreWidth });
			main = out.main;
			restoreWidth = out.restoreWidth;
		}
	}
	assert.equal(main.width, 1512 - STRIP_W);
	// What close() puts back.
	assert.equal(restoreWidth, 1512);
});

test('turning the shrink off gives the width back and forgets it', () => {
	const out = sidePlayerLayout({
		main: { x: 0, y: 25, width: 1512 - PLAYER_W, height: 891 },
		workArea: SCREEN,
		collapsed: false,
		shrinkMain: false,
		restoreWidth: 1512
	});
	assert.equal(out.main.width, 1512);
	assert.equal(out.restoreWidth, null);
});

test('with shrinking off, Open DJ keeps its size and the player overlaps on screen', () => {
	const main = { x: 0, y: 25, width: 1512, height: 891 };
	const out = sidePlayerLayout({ main, workArea: SCREEN, collapsed: false, shrinkMain: false, restoreWidth: null });
	assert.deepEqual(out.main, main);
	assert.equal(out.player.x + out.player.width, 1512);
});

test('Open DJ never shrinks below its minimum', () => {
	const tiny = { x: 0, y: 0, width: 1100, height: 800 };
	const main = { x: 0, y: 0, width: 1100, height: 800 };
	const out = sidePlayerLayout({ main, workArea: tiny, collapsed: false, shrinkMain: true, restoreWidth: null });
	assert.equal(out.main.width, MAIN_MIN_W);
	assert.ok(out.player.x + out.player.width <= 1100);
});

test('only Spotify pages load in the player; only open.spotify.com is openable from Open DJ', () => {
	assert.ok(isSidePlayerUrl('https://accounts.spotify.com/login'));
	assert.ok(isSidePlayerOpenTarget('https://open.spotify.com/playlist/abc'));
	assert.ok(!isSidePlayerOpenTarget('https://accounts.spotify.com/login'));
	// Only the four content types, each with an id.
	for (const kind of ['playlist', 'album', 'track', 'artist']) {
		assert.ok(isSidePlayerOpenTarget(`https://open.spotify.com/${kind}/4uLU6hMCjMI75M1A2tKUQC?si=x`), kind);
	}
	for (const bad of [
		'https://open.spotify.com/',
		'https://open.spotify.com/playlist/',
		'https://open.spotify.com/playlist',
		'https://open.spotify.com/account/overview',
		'https://open.spotify.com/user/someone',
		'https://open.spotify.com/playlist/abc/extra',
		'https://open.spotify.com/playlist/a%2Fb'
	]) {
		assert.ok(!isSidePlayerOpenTarget(bad), bad);
	}
	assert.ok(!isSidePlayerUrl('http://open.spotify.com/'));
	assert.ok(!isSidePlayerUrl('https://open.spotify.com.evil.example/'));
	assert.ok(!isSidePlayerUrl('file:///etc/passwd'));
});

// ----- Chrome sign-ins --------------------------------------------------------
// A cookie database written by a real Chromium (fixtures/chrome-141, see its
// capture.mjs and MANIFEST.json), never one this suite builds itself: a
// hand-made table encrypted by our own code would stay green against a Chrome
// whose schema or format the importer cannot read.
const CHROME_DB = path.join(FIXTURES, 'chrome-141', 'Cookies');
const CHROME_MANIFEST = JSON.parse(fs.readFileSync(path.join(FIXTURES, 'chrome-141', 'MANIFEST.json'), 'utf8')) as {
	sha256: string;
	writer: string;
	captured_unix: number;
	cookie_db_version: number;
};
// The fixture's values are sealed under a fixture-only password with the
// macOS derivation (fixtures/chrome-141/rekey.mjs); Chromium's own Linux
// basic-store key is never written here (research rule R7).
const FIXTURE_PASSWORD = 'open-dj-chrome-fixture';
const KEY = chromeKeyFromPassword(FIXTURE_PASSWORD);
// Pinned here, outside the fixture directory, so regenerating the database
// and its manifest together cannot pass unnoticed.
const FIXTURE_SHA256 = 'cc9e93d68dbe4c18edf0d6d8a1940bd2d3d390ca7f20270ed6ec72a82bfbf66f';
const FIXTURE_WRITER = 'Chromium 141.0.7390.37';
// The fixture's cookies expire 400 days after capture, so read it as of then.
const AT_CAPTURE = { nowUnix: CHROME_MANIFEST.captured_unix };

function fixtureRow(hostKey: string, name: string): Buffer {
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	const db = new DatabaseSync(CHROME_DB, { readOnly: true });
	const row = db.prepare('SELECT encrypted_value FROM cookies WHERE host_key = ? AND name = ?').get(hostKey, name) as
		| { encrypted_value: Uint8Array }
		| undefined;
	db.close();
	assert.ok(row, `${hostKey} ${name} is in the fixture`);
	return Buffer.from(row.encrypted_value);
}

test('the Chrome cookie fixture is the captured file, byte for byte', () => {
	const digest = createHash('sha256').update(fs.readFileSync(CHROME_DB)).digest('hex');
	assert.equal(digest, FIXTURE_SHA256);
	assert.equal(CHROME_MANIFEST.sha256, FIXTURE_SHA256);
	assert.ok(CHROME_MANIFEST.writer.startsWith(`${FIXTURE_WRITER} `), CHROME_MANIFEST.writer);
	assert.equal(CHROME_MANIFEST.cookie_db_version, 24);
});

test("decrypts Chrome's own host-bound (DB 24) values and refuses the rest", () => {
	const spotify = fixtureRow('.spotify.com', 'sp_dc');
	assert.equal(decryptChromeValue(spotify, KEY, '.spotify.com', 24), 'fixture-spotify-value');
	// The same real value presented for another host fails Chrome's host binding.
	assert.throws(() => decryptChromeValue(spotify, KEY, '.evil.example', 24), /host check/);
	assert.throws(() => decryptChromeValue(Buffer.from('v20abcdef'), KEY, '.spotify.com', 24), /unsupported/);
	// The same password under the Linux derivation is a different key: it must
	// not open values sealed the macOS way.
	assert.throws(() => decryptChromeValue(spotify, pbkdf2Sync(FIXTURE_PASSWORD, 'saltysalt', 1, 16, 'sha1'), '.spotify.com', 24));
});

test('a decrypted value that is not UTF-8 fails instead of being stored altered', () => {
	// Chrome's own scheme: AES-128-CBC with a 16-space IV over the host digest
	// and the value. Valid padding and digest, value bytes that are not UTF-8.
	const seal = (value: Buffer): Buffer => {
		const cipher = createCipheriv('aes-128-cbc', KEY, Buffer.alloc(16, ' '));
		const digest = createHash('sha256').update('.spotify.com').digest();
		return Buffer.concat([Buffer.from('v10'), cipher.update(Buffer.concat([digest, value])), cipher.final()]);
	};
	// The control: the same sealing of a UTF-8 value reads back intact.
	assert.equal(decryptChromeValue(seal(Buffer.from('caf\u00e9')), KEY, '.spotify.com', 24), 'caf\u00e9');
	assert.throws(() => decryptChromeValue(seal(Buffer.from([0x61, 0xff, 0xfe])), KEY, '.spotify.com', 24), TypeError);
});

test('only listed music sites match, by whole label', () => {
	assert.equal(hostIsLoginSite('.spotify.com'), 'spotify.com');
	assert.equal(hostIsLoginSite('accounts.spotify.com'), 'spotify.com');
	assert.equal(hostIsLoginSite('.notspotify.com'), null);
	assert.equal(hostIsLoginSite('mail.google.com'), null);
	assert.ok(LOGIN_SITES.includes('beatport.com'));
});

test('host-only cookies stay host-only and expired ones are dropped', () => {
	const base = { name: 'n', value: '', encrypted_value: new Uint8Array(), path: '/', is_secure: 1, is_httponly: 1, samesite: 1, source_scheme: 2, source_port: 443 };
	const future = (Date.now() / 1000 + 3600 + 11644473600) * 1_000_000;
	const hostOnly = toImportedCookie({ ...base, host_key: 'open.spotify.com', expires_utc: future }, 'v', Date.now() / 1000);
	assert.ok(hostOnly !== null);
	assert.equal(hostOnly.domain, undefined);
	assert.equal(hostOnly.url, 'https://open.spotify.com/');
	assert.equal(hostOnly.sameSite, 'lax');
	const domain = toImportedCookie({ ...base, host_key: '.spotify.com', expires_utc: 0 }, 'v', Date.now() / 1000);
	assert.equal(domain?.domain, '.spotify.com');
	assert.equal(domain?.expirationDate, undefined);
	const expired = toImportedCookie({ ...base, host_key: '.spotify.com', expires_utc: 13_000_000_000_000_000 }, 'v', Date.now() / 1000);
	assert.equal(expired, null);
	assert.ok(Math.abs((chromeTimeToUnix(11644473600 * 1_000_000) ?? -1) - 0) < 1e-6);
});

test('reads a Cookies database Chromium wrote and imports only music sites', () => {
	const result = readChromeLoginCookies(CHROME_DB, KEY, AT_CAPTURE);
	const byName = new Map(result.cookies.map((c) => [c.name, c]));
	assert.equal(byName.get('sp_dc')?.value, 'fixture-spotify-value');
	assert.equal(byName.get('sp_dc')?.domain, '.spotify.com');
	assert.equal(byName.get('session')?.value, 'fixture-beatport-value');
	assert.equal(byName.get('session')?.sameSite, 'strict');
	// Chrome's host-only form (no leading dot) stays host-only.
	assert.equal(byName.get('host_only')?.domain, undefined);
	assert.equal(byName.get('host_only')?.url, 'https://open.spotify.com/');
	assert.equal(byName.has('SID'), false);
	assert.deepEqual(result.sites, ['beatport.com', 'spotify.com']);
	assert.equal(result.skipped, 0);
	// A wrong key fails the import loudly instead of reporting zero cookies
	// imported.
	assert.throws(
		() => readChromeLoginCookies(CHROME_DB, chromeKeyFromPassword('wrong'), AT_CAPTURE),
		/3 of Chrome's encrypted cookies could not be decrypted/
	);
	// Read after the 400-day lifetime Chrome gave them, every cookie is expired.
	const later = readChromeLoginCookies(CHROME_DB, KEY, { nowUnix: CHROME_MANIFEST.captured_unix + 401 * 86400 });
	assert.equal(later.cookies.length, 0);
});

test('partitioned (CHIPS) cookies are never imported, so they cannot replace the login cookie', () => {
	const dir = scratchDir('chrome-partitioned');
	const db = path.join(dir, 'Cookies');
	fs.copyFileSync(CHROME_DB, db);
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	const w = new DatabaseSync(db);
	const copySpDc = (set: string): void => {
		w.exec("DROP TABLE IF EXISTS temp.t; CREATE TEMP TABLE t AS SELECT * FROM cookies WHERE host_key = '.spotify.com' AND name = 'sp_dc'");
		w.exec(`UPDATE temp.t SET encrypted_value = X'', ${set}`);
		w.exec('INSERT INTO cookies SELECT * FROM temp.t');
	};
	// The same name, domain and path, partitioned under another top-level site.
	copySpDc("value = 'partitioned-value', top_frame_site_key = 'https://example.com', has_cross_site_ancestor = 1");
	// Control: an ordinary row added the same way is imported, so the copy itself works.
	copySpDc("value = 'unpartitioned-value', path = '/control'");
	w.close();
	const result = readChromeLoginCookies(db, KEY, AT_CAPTURE);
	const spDc = result.cookies.filter((c) => c.name === 'sp_dc');
	assert.deepEqual(spDc.map((c) => c.value).sort(), ['fixture-spotify-value', 'unpartitioned-value']);
	assert.equal(result.cookies.length, 4);
	assert.equal(result.skipped, 0);
});

test('a partitioned row with both values fails the import before it is dropped', () => {
	// SPOT-6555c checks the contradiction before any skip, partitioned rows
	// included.
	const dir = scratchDir('chrome-partitioned-both');
	const db = path.join(dir, 'Cookies');
	fs.copyFileSync(CHROME_DB, db);
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	const w = new DatabaseSync(db);
	w.exec("CREATE TEMP TABLE t AS SELECT * FROM cookies WHERE host_key = '.spotify.com' AND name = 'sp_dc'");
	// Keeps the real ciphertext and adds a plaintext beside it.
	w.exec("UPDATE temp.t SET value = 'plain-too', top_frame_site_key = 'https://example.com', has_cross_site_ancestor = 1");
	w.exec('INSERT INTO cookies SELECT * FROM temp.t');
	w.close();
	assert.throws(() => readChromeLoginCookies(db, KEY, AT_CAPTURE), /has both a plain and an encrypted value/);
});

test('a damaged encrypted row refuses the import; another-host and unknown-scheme rows are skipped', () => {
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	const withRows = (label: string, sets: string[]): string => {
		const db = path.join(scratchDir(`chrome-partial-${label}`), 'Cookies');
		fs.copyFileSync(CHROME_DB, db);
		const w = new DatabaseSync(db);
		for (const set of sets) {
			w.exec("DROP TABLE IF EXISTS temp.t; CREATE TEMP TABLE t AS SELECT * FROM cookies WHERE host_key = '.spotify.com' AND name = 'sp_dc' ORDER BY rowid LIMIT 1");
			w.exec(`UPDATE temp.t SET ${set}`);
			w.exec('INSERT INTO cookies SELECT * FROM temp.t');
		}
		w.close();
		return db;
	};
	// Chrome's real sp_dc value moved to another music host fails the host
	// binding, which Chrome itself drops; a newer scheme is unknown here.
	const skippable = withRows('skips', ["host_key = 'open.spotify.com', path = '/moved'", "path = '/newer', encrypted_value = X'763230aabbcc'"]);
	const result = readChromeLoginCookies(skippable, KEY, AT_CAPTURE);
	assert.equal(result.cookies.length, 3);
	assert.equal(result.skipped, 2);
	// A v10 value whose ciphertext is damaged, beside good rows, is not a
	// success missing one cookie.
	const damaged = withRows('damaged', ["path = '/damaged', encrypted_value = X'7631300102030405060708090a0b0c0d0e0f10'"]);
	assert.throws(() => readChromeLoginCookies(damaged, KEY, AT_CAPTURE), /^Error: 1 of Chrome's encrypted cookies could not be decrypted/);
	// A row carrying both a plain value and a ciphertext is damaged, whatever
	// the ciphertext holds.
	const both = withRows('both', ["path = '/both', value = 'plain'"]);
	assert.throws(
		() => readChromeLoginCookies(both, KEY, AT_CAPTURE),
		/^Error: Chrome cookie sp_dc for \.spotify\.com has both a plain and an encrypted value; nothing was imported\.$/
	);
	// The contradiction fails even on a row whose SameSite is unknown, which
	// would otherwise be skipped before the check.
	const bothUnknown = withRows('both-unknown', ["path = '/both-unknown', value = 'plain', samesite = 7"]);
	assert.throws(() => readChromeLoginCookies(bothUnknown, KEY, AT_CAPTURE), /has both a plain and an encrypted value/);
	// Control: an unknown SameSite on its own is still a skip.
	const unknownOnly = withRows('unknown-only', ["path = '/unknown-only', samesite = 7"]);
	assert.equal(readChromeLoginCookies(unknownOnly, KEY, AT_CAPTURE).skipped, 1);
	// With the wrong key every row fails, so the import fails as a whole.
	assert.throws(() => readChromeLoginCookies(skippable, chromeKeyFromPassword('wrong'), AT_CAPTURE), /could not be decrypted/);
	// A host-check failure with nothing else verifying the key is what a wrong
	// key that happened to pass the padding check looks like, so it fails too
	// rather than importing zero cookies.
	const onlyMoved = withRows('only-moved', ["host_key = 'open.spotify.com', path = '/moved'"]);
	const w = new DatabaseSync(onlyMoved);
	w.exec("DELETE FROM cookies WHERE path = '/'");
	w.close();
	assert.throws(
		() => readChromeLoginCookies(onlyMoved, KEY, AT_CAPTURE),
		/^Error: None of Chrome's encrypted cookies verified .*\(1 failed the host check\); nothing was imported\.$/
	);
});

test('a database from before Chrome bound values to their site refuses encrypted rows', () => {
	// Before version 24 no host digest exists, so a wrong key that keeps its
	// padding and UTF-8 would import garbage as verified.
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	const db = path.join(scratchDir('chrome-v23'), 'Cookies');
	fs.copyFileSync(CHROME_DB, db);
	const w = new DatabaseSync(db);
	w.exec("UPDATE meta SET value = '23' WHERE key = 'version'");
	w.close();
	assert.throws(
		() => readChromeLoginCookies(db, KEY, AT_CAPTURE),
		/^Error: Chrome's cookie database is version 23, from before Chrome bound each cookie to its site.*Nothing was imported\.$/
	);
	// Control: the same rows at version 24 import.
	assert.equal(readChromeLoginCookies(CHROME_DB, KEY, AT_CAPTURE).cookies.length, 3);
});

test('a database from before Chrome recorded cookie sources still imports', () => {
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	const legacy = path.join(scratchDir('chrome-no-source-columns'), 'Cookies');
	fs.copyFileSync(CHROME_DB, legacy);
	const w = new DatabaseSync(legacy);
	w.exec('DROP INDEX cookies_unique_index');
	w.exec('ALTER TABLE cookies DROP COLUMN source_port');
	w.exec('ALTER TABLE cookies DROP COLUMN source_scheme');
	w.close();
	const result = readChromeLoginCookies(legacy, KEY, AT_CAPTURE);
	// The same cookies as the current schema, their scheme from the Secure flag.
	assert.deepEqual(
		result.cookies.map((c) => c.url).sort(),
		readChromeLoginCookies(CHROME_DB, KEY, AT_CAPTURE).cookies.map((c) => c.url).sort()
	);
	assert.equal(result.cookies.length, 3);
});

test('a cookie row with a damaged flag or path fails instead of being coerced', () => {
	const row = {
		host_key: 'open.spotify.com',
		name: 'x',
		value: 'v',
		encrypted_value: new Uint8Array(),
		path: '/p',
		expires_utc: 0,
		is_secure: 1,
		is_httponly: 1,
		samesite: 1,
		source_scheme: 2,
		source_port: 443
	};
	// Control: the exact values map.
	const ok = toImportedCookie(row, 'v', 0);
	assert.equal(ok?.url, 'https://open.spotify.com/p');
	assert.equal(ok?.path, '/p');
	assert.equal(toImportedCookie({ ...row, is_secure: 0, is_httponly: 0 }, 'v', 0)?.secure, false);
	// The source origin is Chrome's, scheme and port: an
	// https-set cookie without Secure stays https, a port-bound one keeps it.
	assert.equal(toImportedCookie({ ...row, is_secure: 0 }, 'v', 0)?.url, 'https://open.spotify.com/p');
	assert.equal(toImportedCookie({ ...row, is_secure: 0, source_scheme: 1, source_port: 80 }, 'v', 0)?.url, 'http://open.spotify.com/p');
	assert.equal(toImportedCookie({ ...row, source_port: 8443 }, 'v', 0)?.url, 'https://open.spotify.com:8443/p');
	assert.equal(toImportedCookie({ ...row, is_secure: 0, source_scheme: 1, source_port: 443 }, 'v', 0)?.url, 'http://open.spotify.com:443/p');
	assert.equal(toImportedCookie({ ...row, source_port: -1 }, 'v', 0)?.url, 'https://open.spotify.com/p');
	// Unset (rows older than the column) falls back to the Secure flag, as Chrome does.
	assert.equal(toImportedCookie({ ...row, source_scheme: 0, source_port: -1 }, 'v', 0)?.url, 'https://open.spotify.com/p');
	assert.equal(toImportedCookie({ ...row, is_secure: 0, source_scheme: 0, source_port: -1 }, 'v', 0)?.url, 'http://open.spotify.com/p');
	for (const [label, patch, problem] of [
		['secure 2', { is_secure: 2 }, /is_secure 2 .* is not 0 or 1/],
		['secure NaN', { is_secure: Number.NaN }, /is_secure NaN .* is not 0 or 1/],
		['httponly -1', { is_httponly: -1 }, /is_httponly -1 .* is not 0 or 1/],
		['empty path', { path: '' }, /path "" .* is not an absolute path/],
		['relative path', { path: 'p' }, /path "p" .* is not an absolute path/],
		['scheme 3', { source_scheme: 3 }, /unknown Chrome source_scheme 3/],
		['secure over http', { source_scheme: 1 }, /is Secure but was set over http/],
		['port 0', { source_port: 0 }, /source_port 0 .* is not a port/],
		['port 70000', { source_port: 70000 }, /source_port 70000 .* is not a port/]
	] as const) {
		assert.throws(() => toImportedCookie({ ...row, ...patch }, 'v', 0), problem, label);
	}
	// An expired row is checked before it is dropped; the
	// control shows the same expired row with a good origin is only dropped.
	const expiredAt = { ...row, expires_utc: 13_000_000_000_000_000 };
	const now = Date.now() / 1000;
	assert.equal(toImportedCookie(expiredAt, 'v', now), null);
	assert.throws(() => toImportedCookie({ ...expiredAt, source_scheme: 3 }, 'v', now), /unknown Chrome source_scheme 3/);
	assert.throws(() => toImportedCookie({ ...expiredAt, source_scheme: 1 }, 'v', now), /is Secure but was set over http/);
	assert.throws(() => toImportedCookie({ ...expiredAt, source_port: 0 }, 'v', now), /source_port 0 .* is not a port/);
	// A text flag in the database is refused, not read as NaN or 0.
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	const db = path.join(scratchDir('chrome-text-flag'), 'Cookies');
	fs.copyFileSync(CHROME_DB, db);
	const w = new DatabaseSync(db);
	w.exec("CREATE TEMP TABLE t AS SELECT * FROM cookies WHERE host_key = '.spotify.com' AND name = 'sp_dc' ORDER BY rowid LIMIT 1");
	w.exec("UPDATE temp.t SET path = '/text', is_secure = 'yes'");
	w.exec('INSERT INTO cookies SELECT * FROM temp.t');
	w.close();
	assert.throws(() => readChromeLoginCookies(db, KEY, AT_CAPTURE), /is_secure "yes" .* is not an integer/);
});

test('a damaged expiry fails the import instead of reading as a session cookie', () => {
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	// Chrome declares expires_utc INTEGER NOT NULL, so NULL cannot be stored, and
	// INTEGER affinity stores numeric text or an integral REAL as an integer.
	// What a damaged row can still hold is text, a fractional REAL or a negative.
	for (const [label, value] of [
		['empty text', "''"],
		['text', "'soon'"],
		['a fractional REAL', '1.5'],
		['negative', '-1']
	] as const) {
		const db = path.join(scratchDir(`chrome-expiry-${label.replace(/\W+/g, '-')}`), 'Cookies');
		fs.copyFileSync(CHROME_DB, db);
		const w = new DatabaseSync(db);
		w.exec("CREATE TEMP TABLE t AS SELECT * FROM cookies WHERE host_key = '.spotify.com' AND name = 'sp_dc' ORDER BY rowid LIMIT 1");
		w.exec(`UPDATE temp.t SET path = '/expiry', expires_utc = ${value}`);
		w.exec('INSERT INTO cookies SELECT * FROM temp.t');
		w.close();
		assert.throws(() => readChromeLoginCookies(db, KEY, AT_CAPTURE), /expires_utc .* is not a non-negative integer/, label);
	}
	// Controls: an INTEGER 0 is a session cookie and a future INTEGER keeps its expiry.
	for (const [label, value] of [
		['session', '0'],
		['future', '20000000000000000']
	] as const) {
		const db = path.join(scratchDir(`chrome-expiry-ok-${label}`), 'Cookies');
		fs.copyFileSync(CHROME_DB, db);
		const w = new DatabaseSync(db);
		w.exec("CREATE TEMP TABLE t AS SELECT * FROM cookies WHERE host_key = '.spotify.com' AND name = 'sp_dc' ORDER BY rowid LIMIT 1");
		w.exec(`UPDATE temp.t SET path = '/expiry', expires_utc = ${value}`);
		w.exec('INSERT INTO cookies SELECT * FROM temp.t');
		w.close();
		const cookie = readChromeLoginCookies(db, KEY, AT_CAPTURE).cookies.find((c) => c.path === '/expiry');
		assert.ok(cookie, label);
		assert.equal(cookie.expirationDate === undefined, label === 'session', label);
	}
	assert.throws(() => chromeTimeToUnix(1.5), /is not an integer/);
});

test('a row with an unknown SameSite value is skipped, never imported as unspecified', () => {
	const dir = scratchDir('chrome-samesite');
	const db = path.join(dir, 'Cookies');
	fs.copyFileSync(CHROME_DB, db);
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	const w = new DatabaseSync(db);
	w.exec("CREATE TEMP TABLE t AS SELECT * FROM cookies WHERE host_key = '.spotify.com' AND name = 'sp_dc' ORDER BY rowid LIMIT 1");
	w.exec("UPDATE temp.t SET path = '/future', samesite = 7");
	w.exec('INSERT INTO cookies SELECT * FROM temp.t');
	w.close();
	const result = readChromeLoginCookies(db, KEY, AT_CAPTURE);
	assert.equal(result.cookies.length, 3);
	assert.equal(result.skipped, 1);
	assert.equal(result.cookies.some((c) => c.path === '/future'), false);
	const row = {
		host_key: 'open.spotify.com',
		name: 'x',
		value: 'v',
		encrypted_value: new Uint8Array(),
		path: '/',
		expires_utc: 0,
		is_secure: 1,
		is_httponly: 0,
		samesite: 7,
		source_scheme: 2,
		source_port: 443
	};
	assert.throws(() => toImportedCookie(row, 'v', 0), /unknown Chrome SameSite value 7/);
	// Control: every known value still maps.
	assert.equal(toImportedCookie({ ...row, samesite: 1 }, 'v', 0)?.sameSite, 'lax');
	assert.equal(toImportedCookie({ ...row, samesite: -1 }, 'v', 0)?.sameSite, 'unspecified');
});

test('a missing or malformed database version fails instead of skipping the host check', () => {
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	for (const [label, sql] of [
		['missing', "DELETE FROM meta WHERE key = 'version'"],
		['malformed', "UPDATE meta SET value = 'twenty-four' WHERE key = 'version'"],
		['fractional', "UPDATE meta SET value = '24.5' WHERE key = 'version'"],
		['empty', "UPDATE meta SET value = '' WHERE key = 'version'"],
		['blank', "UPDATE meta SET value = '   ' WHERE key = 'version'"],
		['null', "UPDATE meta SET value = NULL WHERE key = 'version'"]
	] as const) {
		const db = path.join(scratchDir(`chrome-version-${label}`), 'Cookies');
		fs.copyFileSync(CHROME_DB, db);
		const w = new DatabaseSync(db);
		w.exec(sql);
		w.close();
		assert.throws(() => readChromeLoginCookies(db, KEY, AT_CAPTURE), /no readable version/, label);
	}
});

test('a cookie the store rejects makes the import a refusal that says so', async () => {
	const cookie = (name: string): ReturnType<typeof toImportedCookie> & object => ({
		url: 'https://open.spotify.com/',
		name,
		value: 'v',
		path: '/',
		secure: true,
		httpOnly: true,
		sameSite: 'lax'
	});
	const result = { cookies: [cookie('a'), cookie('b')], sites: ['spotify.com'], skipped: 0 };
	const stored: string[] = [];
	let flushed = 0;
	const store = (reject: Set<string>) => ({
		set: async (c: { name: string }) => {
			if (reject.has(c.name)) throw new Error('Failed to set cookie');
			stored.push(c.name);
		},
		flushStore: async () => {
			flushed += 1;
		}
	});
	const refused = await storeImportedCookies(store(new Set(['b'])), result);
	assert.equal(refused.kind, 'refused');
	assert.match((refused as { reason: string }).reason, /refused 1 of 2 .*open\.spotify\.com b: Failed to set cookie/);
	const allRefused = await storeImportedCookies(store(new Set(['a', 'b'])), result);
	assert.match((allRefused as { reason: string }).reason, /refused 2 of 2/);
	// Control: a store that takes every cookie reports the import.
	assert.deepEqual(await storeImportedCookies(store(new Set()), result), {
		kind: 'imported',
		browser: 'Google Chrome',
		cookies: 2,
		sites: ['spotify.com']
	});
	assert.equal(flushed, 3);
});

test('the Chrome profile comes from Local State, and is refused when that cannot say', () => {
	const home = scratchDir('chrome-profile');
	const root = path.join(home, 'Library', 'Application Support', 'Google', 'Chrome');
	const cookiesIn = (profile: string): string => {
		const file = path.join(root, profile, 'Network', 'Cookies');
		fs.mkdirSync(path.dirname(file), { recursive: true });
		fs.writeFileSync(file, '', { encoding: 'utf8' });
		return file;
	};
	// Portable text: explicit UTF-8 and an LF ending.
	const localState = (body: string): void => fs.writeFileSync(path.join(root, 'Local State'), `${body}\n`, { encoding: 'utf8' });
	const defaultCookies = cookiesIn('Default');
	const workCookies = cookiesIn('Profile 2');

	// No Chrome at all.
	assert.deepEqual(chromeProfileCookies(home), { kind: 'refused', reason: 'No Google Chrome profile was found on this Mac.' });
	// The last-used profile wins, even with Default present.
	localState(JSON.stringify({ profile: { last_used: 'Profile 2', info_cache: { Default: {}, 'Profile 2': {} } } }));
	assert.deepEqual(chromeProfileCookies(home), { kind: 'found', cookies: workCookies });
	// A sole listed profile is unambiguous without a last-used entry.
	localState(JSON.stringify({ profile: { info_cache: { Default: {} } } }));
	assert.deepEqual(chromeProfileCookies(home), { kind: 'found', cookies: defaultCookies });
	// Two profiles and no last-used entry: never a guess at Default.
	localState(JSON.stringify({ profile: { info_cache: { Default: {}, 'Profile 2': {} } } }));
	assert.equal(chromeProfileCookies(home).kind, 'refused');
	// A damaged Local State is refused, not read as Default.
	localState('{"profile": {"last_used": "Prof');
	assert.match((chromeProfileCookies(home) as { reason: string }).reason, /damaged/);
	// Malformed profile maps are damaged too: an array, a
	// missing or non-record profile list, or a last_used it does not name.
	for (const body of [
		[],
		{ profile: [] },
		{ profile: { last_used: 'Profile 2' } },
		{ profile: { last_used: 'Profile 2', info_cache: 'Profile 2' } },
		{ profile: { last_used: '0', info_cache: ['Default'] } }
	]) {
		localState(JSON.stringify(body));
		assert.match((chromeProfileCookies(home) as { reason: string }).reason, /damaged/, JSON.stringify(body));
	}
	localState(JSON.stringify({ profile: { last_used: 'Profile 2', info_cache: { Default: {} } } }));
	assert.match((chromeProfileCookies(home) as { reason: string }).reason, /"Profile 2" is not in its profile list/);
	// An unreadable one (a directory in its place) is refused with the cause.
	fs.rmSync(path.join(root, 'Local State'));
	fs.mkdirSync(path.join(root, 'Local State'));
	assert.match((chromeProfileCookies(home) as { reason: string }).reason, /could not be read \(EISDIR\)/);
});

test('a cookie database that changes mid-copy is retaken, and one that never settles fails', () => {
	const dir = scratchDir('chrome-snapshot');
	const live = path.join(dir, 'Cookies');
	fs.copyFileSync(CHROME_DB, live);
	const dest = path.join(dir, 'snapshot');
	// Chrome writing its WAL between our copies of the main file and the WAL.
	let writes = 1;
	const racing = (from: string, to: string): void => {
		fs.copyFileSync(from, to);
		if (writes > 0 && from === live) {
			writes -= 1;
			fs.appendFileSync(`${live}-wal`, Buffer.alloc(4096));
		}
	};
	snapshotSqlite(live, dest, racing);
	assert.equal(writes, 0);
	// The kept copy is the retaken one, WAL included, byte for byte.
	assert.deepEqual(fs.readFileSync(`${dest}-wal`), fs.readFileSync(`${live}-wal`));

	writes = Number.POSITIVE_INFINITY;
	assert.throws(() => snapshotSqlite(live, dest, racing), /kept writing/);
	// Control: a database nobody is writing is copied on the first try.
	fs.rmSync(`${live}-wal`);
	let copies = 0;
	snapshotSqlite(live, dest, (from, to) => {
		copies += 1;
		fs.copyFileSync(from, to);
	});
	assert.equal(copies, 1);
	assert.equal(fs.existsSync(`${dest}-wal`), false);
	// A checkpoint that removes the -wal between the signature and its copy is
	// retaken, not a failure.
	fs.copyFileSync(CHROME_DB, `${live}-wal`);
	let removed = false;
	const checkpointing = (from: string, to: string): void => {
		if (!removed && from === `${live}-wal`) {
			removed = true;
			fs.rmSync(from);
		}
		fs.copyFileSync(from, to);
	};
	snapshotSqlite(live, dest, checkpointing);
	assert.equal(removed, true);
	assert.equal(fs.existsSync(`${dest}-wal`), false);
	// Control: a main database that vanishes is still an error.
	assert.throws(
		() =>
			snapshotSqlite(live, dest, (from) => {
				throw Object.assign(new Error(`ENOENT: ${from}`), { code: 'ENOENT' });
			}),
		/ENOENT/
	);
	// A -wal that exists but cannot be inspected aborts, never reads as absent.
	fs.symlinkSync(`${live}-wal`, `${live}-wal`);
	assert.throws(() => snapshotSqlite(live, dest), /ELOOP/);
});

test('a torn database copy fails the import instead of dropping rows', () => {
	const dir = scratchDir('chrome-torn');
	const torn = path.join(dir, 'Cookies');
	const bytes = Buffer.from(fs.readFileSync(CHROME_DB));
	// Clobber the last page, where Chrome's cookie rows live, keeping the header valid.
	bytes.fill(0xa5, bytes.length - 2048);
	fs.writeFileSync(torn, bytes);
	assert.throws(() => readChromeLoginCookies(torn, KEY, AT_CAPTURE));
	// Control: the intact fixture passes the same check.
	assert.equal(readChromeLoginCookies(CHROME_DB, KEY, AT_CAPTURE).cookies.length, 3);
});

test('the cookie query itself returns only listed music sites, never another site', () => {
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
	const db = new DatabaseSync(':memory:');
	db.exec('CREATE TEMP TABLE cookies(host_key TEXT NOT NULL)');
	const hosts = [
		'.spotify.com',
		'spotify.com',
		'accounts.spotify.com',
		'.Beatport.com',
		'.google.com',
		'evilspotify.com',
		'spotify.com.evil.example',
		'spotifyxcom'
	];
	const insert = db.prepare('INSERT INTO cookies(host_key) VALUES (?)');
	for (const host of hosts) insert.run(host);
	const { where, params } = loginSiteWhere(LOGIN_SITES);
	const got = (db.prepare(`SELECT host_key FROM cookies WHERE ${where}`).all(...params) as { host_key: string }[])
		.map((r) => r.host_key)
		.sort();
	assert.deepEqual(got, ['.Beatport.com', '.spotify.com', 'accounts.spotify.com', 'spotify.com']);
	// Same verdict as the in-process filter for every host.
	for (const host of hosts) assert.equal(got.includes(host), hostIsLoginSite(host) !== null, host);
	// A site name with LIKE wildcards matches literally.
	const odd = loginSiteWhere(['a_b.com']);
	insert.run('x.aXb.com');
	insert.run('x.a_b.com');
	const oddGot = (db.prepare(`SELECT host_key FROM cookies WHERE ${odd.where}`).all(...odd.params) as { host_key: string }[]).map(
		(r) => r.host_key
	);
	assert.deepEqual(oddGot, ['x.a_b.com']);
	assert.deepEqual(loginSiteWhere([]), { where: '0', params: [] });
	db.close();
});

test('Windows and Linux say why instead of guessing', () => {
	assert.equal(unsupportedPlatformReason('darwin'), null);
	assert.match(unsupportedPlatformReason('win32') ?? '', /Chrome 127 and later on Windows encrypt sign-ins so that only Chrome can read them \(Google, Jul 2024\)/);
	assert.match(unsupportedPlatformReason('linux') ?? '', /only supported on macOS/);
});

test('the player presents plain Chrome, without Electron or app tokens', () => {
	const ua =
		'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) opendj-desktop-electron/1.0.0-alpha.1 Chrome/146.0.0.0 Electron/44.4.5 Safari/537.36';
	const plain = plainChromeUserAgent(ua);
	assert.equal(
		plain,
		'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36'
	);
});

test('side player IPC arguments are checked exactly, never coerced', () => {
	// Controls: the shapes the preload sends.
	assert.deepEqual(sideOpenArgs('https://open.spotify.com/', { shrinkMain: true }), { url: 'https://open.spotify.com/', shrinkMain: true });
	assert.deepEqual(sideOpenArgs('https://open.spotify.com/', { shrinkMain: undefined }), { url: 'https://open.spotify.com/', shrinkMain: undefined });
	assert.deepEqual(sideOpenArgs('https://open.spotify.com/', undefined), { url: 'https://open.spotify.com/', shrinkMain: undefined });
	assert.equal(sideCollapsedArg(true), true);
	assert.equal(sideCollapsedArg(false), false);
	assert.throws(() => sideOpenArgs(7, undefined), /needs a URL string/);
	assert.throws(() => sideOpenArgs('https://open.spotify.com/', null), /options must be an object/);
	assert.throws(() => sideOpenArgs('https://open.spotify.com/', 'yes'), /options must be an object/);
	assert.throws(() => sideOpenArgs('https://open.spotify.com/', { shrinkMain: 'false' }), /shrinkMain must be a boolean/);
	assert.throws(() => sideOpenArgs('https://open.spotify.com/', { shrinkMain: 0 }), /shrinkMain must be a boolean/);
	for (const value of ['true', 1, 0, null, undefined]) {
		assert.throws(() => sideCollapsedArg(value), /collapsed must be a boolean/, String(value));
	}
});

test('the side player refuses to dock only on native Wayland and docks under X11 or Xwayland', () => {
	const wayland = { WAYLAND_DISPLAY: 'wayland-0', XDG_SESSION_TYPE: 'wayland', DISPLAY: ':0' };
	// Electron's default "auto" hint in a Wayland session runs natively there.
	assert.match(sideDockUnavailableReason('linux', wayland, '', '') ?? '', /Wayland does not allow.*--ozone-platform=x11/);
	assert.match(sideDockUnavailableReason('linux', wayland, '', 'auto') ?? '', /Wayland/);
	assert.match(sideDockUnavailableReason('linux', wayland, '', 'wayland') ?? '', /Wayland/);
	assert.match(sideDockUnavailableReason('linux', { XDG_SESSION_TYPE: 'wayland' }, '', '') ?? '', /Wayland/);
	assert.match(sideDockUnavailableReason('linux', { WAYLAND_DISPLAY: 'wayland-0' }, '', '') ?? '', /Wayland/);
	assert.match(sideDockUnavailableReason('linux', { DISPLAY: ':0' }, 'wayland', '') ?? '', /Wayland/);
	// --ozone-platform=auto picks from the session like the auto hint.
	assert.match(sideDockUnavailableReason('linux', wayland, 'auto', '') ?? '', /Wayland/);
	assert.equal(sideDockUnavailableReason('linux', { DISPLAY: ':0' }, 'auto', ''), null);
	// Xwayland inherits the Wayland variables but docks,
	// whether forced by the platform switch or by the hint.
	assert.equal(sideDockUnavailableReason('linux', wayland, 'x11', ''), null);
	assert.equal(sideDockUnavailableReason('linux', wayland, '', 'x11'), null);
	// An explicit platform wins over the hint and the environment both ways.
	assert.equal(sideDockUnavailableReason('linux', wayland, 'x11', 'wayland'), null);
	assert.match(sideDockUnavailableReason('linux', { DISPLAY: ':0' }, 'wayland', 'x11') ?? '', /Wayland/);
	// Any other explicit platform is not Wayland, whatever the session says.
	assert.equal(sideDockUnavailableReason('linux', wayland, 'headless', ''), null);
	// A plain X11 session docks.
	assert.equal(sideDockUnavailableReason('linux', { DISPLAY: ':0', XDG_SESSION_TYPE: 'x11' }, '', ''), null);
	// Wayland variables mean nothing off Linux.
	assert.equal(sideDockUnavailableReason('darwin', wayland, '', ''), null);
	assert.equal(sideDockUnavailableReason('win32', wayland, '', ''), null);
});

test('the preload hands side-open arguments to the main process exactly as given', () => {
	// The real preload, loaded with only its electron import replaced, so the
	// bridge the page calls is the one under test.
	const invoked: unknown[][] = [];
	let exposed: Record<string, unknown> | null = null;
	const fakeElectron = {
		ipcRenderer: {
			sendSync: (channel: string) => (channel === 'opendj:page-init' ? { engineOrigin: null, shellBuild: {}, perfProbe: false, supervisor: null } : null),
			invoke: (...args: unknown[]) => {
				invoked.push(args);
				return Promise.resolve();
			},
			on: () => {},
			removeListener: () => {}
		},
		contextBridge: {
			executeInMainWorld: () => {},
			exposeInMainWorld: (key: string, api: Record<string, unknown>) => {
				if (key === 'opendjShell') exposed = api;
			}
		}
	};
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const Module = require('node:module') as { _load: (request: string, ...rest: unknown[]) => unknown };
	const realLoad = Module._load;
	const preloadPath = require.resolve('../src/preload');
	Module._load = function (request: string, ...rest: unknown[]) {
		return request === 'electron' ? fakeElectron : realLoad.call(this, request, ...rest);
	};
	try {
		delete require.cache[preloadPath];
		// eslint-disable-next-line @typescript-eslint/no-require-imports
		require(preloadPath);
	} finally {
		Module._load = realLoad;
		delete require.cache[preloadPath];
	}
	assert.ok(exposed, 'the preload exposed opendjShell');
	const sidePlayer = (exposed as { sidePlayer: { open: (url: unknown, options?: unknown) => Promise<void> } }).sidePlayer;
	const url = 'https://open.spotify.com/playlist/abc';
	const malformed: unknown[] = [null, 7, 'yes', { shrinkMain: 'false' }];
	for (const options of malformed) void sidePlayer.open(url, options);
	void sidePlayer.open(url, { shrinkMain: true });
	void sidePlayer.open(url);
	assert.equal(invoked.length, malformed.length + 2);
	const call = (i: number): unknown[] => {
		const args = invoked[i];
		assert.ok(args, `call ${i} reached ipcRenderer.invoke`);
		return args;
	};
	malformed.forEach((options, i) => {
		const [channel, sentUrl, sentOptions] = call(i);
		assert.equal(channel, 'opendj:side-open');
		assert.equal(sentOptions, options, `options ${JSON.stringify(options)} reached the main process unchanged`);
		// And the main-process check refuses each one.
		assert.throws(() => sideOpenArgs(sentUrl, sentOptions), /must be/);
	});
	assert.deepEqual(sideOpenArgs(call(malformed.length)[1], call(malformed.length)[2]), { url, shrinkMain: true });
	assert.deepEqual(sideOpenArgs(call(malformed.length + 1)[1], call(malformed.length + 1)[2]), { url, shrinkMain: undefined });
});

test('the import asks for every cookie of the listed sites, not only sign-ins', () => {
	const { message, detail } = importConsentText(['spotify.com', 'tidal.com']);
	// The read takes every row of a listed site, so the question has to say that.
	assert.match(message, /all of your Chrome cookies/);
	assert.match(detail, /every cookie Chrome keeps for them, not only the ones that keep you signed in/);
	assert.match(detail, /preference and analytics cookies come too/);
	assert.match(detail, /^Sites: spotify\.com, tidal\.com\./);
	// Control: the default names the same sites the read filters on.
	assert.ok(importConsentText().detail.startsWith(`Sites: ${LOGIN_SITES.join(', ')}.`));
	assert.doesNotMatch(message, /sign-ins/);
});

test('a cookie database that cannot be checked is refused, not read as missing', () => {
	const home = scratchDir('chrome-profile-stat');
	const root = path.join(home, 'Library', 'Application Support', 'Google', 'Chrome');
	const profile = path.join(root, 'Default');
	fs.mkdirSync(profile, { recursive: true });
	fs.writeFileSync(path.join(root, 'Local State'), `${JSON.stringify({ profile: { last_used: 'Default', info_cache: { Default: {} } } })}\n`, { encoding: 'utf8' });
	const legacy = path.join(profile, 'Cookies');
	fs.writeFileSync(legacy, '', { encoding: 'utf8' });
	// Control: with no Network directory the legacy database is the real one.
	assert.deepEqual(chromeProfileCookies(home), { kind: 'found', cookies: legacy });
	// A file where the Network directory belongs makes the modern path
	// uninspectable (ENOTDIR); existsSync would say false and fall back to
	// the stale legacy file.
	fs.writeFileSync(path.join(profile, 'Network'), '', { encoding: 'utf8' });
	const lookup = chromeProfileCookies(home);
	assert.equal(lookup.kind, 'refused');
	const reason = (lookup as { reason: string }).reason;
	assert.ok(reason.includes(path.join(profile, 'Network', 'Cookies')), reason);
	assert.match(reason, /could not be checked \(ENOTDIR\); nothing was imported\.$/);
});
