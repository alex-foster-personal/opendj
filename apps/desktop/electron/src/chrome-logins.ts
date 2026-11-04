// "Import logins from Chrome" for the side player (SPOT-6555c).
//
// Reads every cookie for a short list of music sites from the user's
// own Chrome profile on this Mac, so the Spotify side player (and later the
// store windows) start signed in without the user hunting for passwords.
// Nothing leaves the machine: cookies go from Chrome's file straight into the
// side player's own Electron session.
//
// Written from Chrome's documented on-disk format (the os_crypt "v10" scheme
// on macOS: AES-128-CBC, key = PBKDF2-SHA1(Keychain "Chrome Safe Storage"
// password, "saltysalt", 1003 iterations), IV of 16 spaces, and since cookie
// DB version 24 a SHA-256 of the host key in front of the value). No code is
// taken from cmux, whose browser import is GPL-3.0.
//
// Only macOS is supported; Windows and Linux report a reason instead of
// guessing.
// - Windows: Chrome 127 and later encrypt cookies with App-Bound Encryption,
//   which "relies on a privileged service to verify the identity of the
//   requesting application" and "encodes the app's identity into the
//   encrypted data", so a different app cannot decrypt them. Evidence:
//   vendor_doc, Will Harris (Chrome Security Team), "Improving the security
//   of Chrome cookies on Windows", Google Security Blog, Tue 30 Jul 2024,
//   https://security.googleblog.com/2024/07/improving-security-of-chrome-cookies-on.html
//   (checked Sun 18 Oct 2026). Not tested_by_us: no Windows host decrypted a
//   real profile for this PR. The blog also names an enterprise policy,
//   ApplicationBoundEncryptionEnabled, that can turn it off; Open DJ does not
//   look for that case and refuses on every Windows install.
// - Linux: Chrome keeps the key in the desktop keyring (Secret Service or
//   KWallet). This is Open DJ's own scope limit, not a Chrome limitation:
//   no keyring reader was built (evidence: inferred from this file).
//
// This file is free of Electron so node:test covers it directly.

import { execFile } from 'node:child_process';
import { createDecipheriv, createHash, pbkdf2Sync } from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

/** Sites whose sign-in a DJ wants in the side player or the store windows. */
export const LOGIN_SITES: readonly string[] = [
	'spotify.com',
	'soundcloud.com',
	'beatport.com',
	'beatsource.com',
	'bandcamp.com',
	'rekordbox.com',
	'pioneerdj.com',
	'tidal.com',
	'traxsource.com'
];

/**
 * What the import asks before it reads anything (SPOT-6555c). It copies every
 * cookie of the listed sites, not a reviewed sign-in subset: Open DJ cannot
 * tell which cookie each site signs in with, and a guessed allowlist would
 * fail silently when a site renames one. So the question says so, analytics
 * and preference cookies included, and the user agrees to that.
 */
export function importConsentText(sites: readonly string[] = LOGIN_SITES): { message: string; detail: string } {
	return {
		message: 'Copy all of your Chrome cookies for these music sites into Open DJ?',
		detail:
			`Sites: ${sites.join(', ')}.\n\n` +
			'That is every cookie Chrome keeps for them, not only the ones that keep you signed in: ' +
			'Open DJ cannot tell those apart, so the sites\' preference and analytics cookies come too. ' +
			'No other site\'s cookies are read.\n\n' +
			'macOS will ask for your password so Open DJ can read Chrome\'s "Chrome Safe Storage" key. ' +
			'Nothing is sent anywhere; the cookies stay in Open DJ\'s side player on this Mac.'
	};
}

/** Does a cookie's host_key (".spotify.com", "accounts.spotify.com") belong to a listed site? */
export function hostIsLoginSite(hostKey: string, sites: readonly string[] = LOGIN_SITES): string | null {
	const host = hostKey.replace(/^\./, '').toLowerCase();
	for (const site of sites) {
		if (host === site || host.endsWith(`.${site}`)) return site;
	}
	return null;
}

/**
 * SQL that matches exactly what hostIsLoginSite accepts: the site itself, its
 * dot-domain form, or any subdomain. LIKE wildcards in a site are escaped.
 */
export function loginSiteWhere(sites: readonly string[]): { where: string; params: string[] } {
	if (sites.length === 0) return { where: '0', params: [] };
	const clauses: string[] = [];
	const params: string[] = [];
	for (const raw of sites) {
		const site = raw.toLowerCase();
		const like = site.replace(/[\\%_]/g, (c) => `\\${c}`);
		clauses.push("(lower(host_key) = ? OR lower(host_key) = ? OR lower(host_key) LIKE ? ESCAPE '\\')");
		params.push(site, `.${site}`, `%.${like}`);
	}
	return { where: clauses.join(' OR '), params };
}

export const CHROME_SALT = 'saltysalt';
export const CHROME_ITERATIONS = 1003;
const IV = Buffer.alloc(16, 0x20);

export function chromeKeyFromPassword(password: string): Buffer {
	return pbkdf2Sync(password, CHROME_SALT, CHROME_ITERATIONS, 16, 'sha1');
}

/**
 * Plain cookie value from Chrome's encrypted_value. Throws on a prefix it does
 * not know (a newer scheme must fail loudly, never import garbage).
 */
/** A v10 value bound to a different host than its row: Chrome itself drops such a cookie. */
export class ChromeHostCheckError extends Error {}

export function decryptChromeValue(encrypted: Buffer, key: Buffer, hostKey: string, dbVersion: number): string {
	if (encrypted.length === 0) return '';
	const prefix = encrypted.subarray(0, 3).toString('latin1');
	if (prefix !== 'v10') throw new Error(`unsupported Chrome cookie encryption "${prefix}"`);
	const decipher = createDecipheriv('aes-128-cbc', key, IV);
	let plain = Buffer.concat([decipher.update(encrypted.subarray(3)), decipher.final()]);
	if (dbVersion >= 24) {
		const digest = createHash('sha256').update(hostKey).digest();
		if (plain.length < 32 || !plain.subarray(0, 32).equals(digest)) {
			throw new ChromeHostCheckError(`cookie for ${hostKey} failed Chrome's host check`);
		}
		plain = plain.subarray(32);
	}
	// Fatal decoding: a damaged value that kept its padding and host digest
	// must fail, not be stored with replacement characters.
	return new TextDecoder('utf-8', { fatal: true }).decode(plain);
}

/** What Electron's session.cookies.set takes, built from one Chrome row. */
export interface ImportedCookie {
	url: string;
	name: string;
	value: string;
	domain?: string;
	path: string;
	secure: boolean;
	httpOnly: boolean;
	expirationDate?: number;
	sameSite: 'unspecified' | 'no_restriction' | 'lax' | 'strict';
}

/** Chrome stores times as microseconds since 1601-01-01 UTC. */
const WINDOWS_EPOCH_OFFSET_S = 11644473600;

export function chromeTimeToUnix(chromeMicros: number | bigint): number | undefined {
	// Divide while still exact (BigInt), then convert: seconds fit a double.
	if (typeof chromeMicros === 'number' && !Number.isInteger(chromeMicros)) {
		throw new Error(`Chrome time ${chromeMicros} is not an integer`);
	}
	const micros = typeof chromeMicros === 'bigint' ? chromeMicros : BigInt(chromeMicros);
	if (micros <= 0n) return undefined;
	return Number(micros / 1_000_000n) + Number(micros % 1_000_000n) / 1_000_000 - WINDOWS_EPOCH_OFFSET_S;
}

const SAME_SITE: Record<number, ImportedCookie['sameSite']> = {
	[-1]: 'unspecified',
	0: 'no_restriction',
	1: 'lax',
	2: 'strict'
};

export interface ChromeCookieRow {
	host_key: string;
	name: string;
	value: string;
	encrypted_value: Uint8Array;
	path: string;
	expires_utc: number | bigint;
	is_secure: number;
	is_httponly: number;
	samesite: number;
	/** Chrome's CookieSourceScheme: 0 unset, 1 non-secure, 2 secure. */
	source_scheme: number;
	/** The port the cookie was set from; -1 when Chrome did not record one. */
	source_port: number;
	/** CHIPS partition (top-frame site); '' for an ordinary cookie or a database from before partitioning. */
	top_frame_site_key?: string;
	has_expires?: number;
	is_persistent?: number;
}

function chromeFlag(value: number, column: string, hostKey: string): boolean {
	if (value === 1) return true;
	if (value === 0) return false;
	throw new Error(`Chrome cookie ${column} ${String(value)} for ${hostKey} is not 0 or 1`);
}

/** An INTEGER column as a number; a NULL, text or fractional value is a damaged row. */
function chromeInt(value: unknown, column: string, hostKey: unknown): number {
	if (typeof value === 'bigint') return Number(value);
	if (typeof value === 'number' && Number.isInteger(value)) return value;
	throw new Error(`Chrome cookie ${column} ${JSON.stringify(String(value))} for ${String(hostKey)} is not an integer`);
}

/**
 * expires_utc as read with BigInts on: an INTEGER arrives as a bigint, so a
 * number here is a REAL, and NULL or text is a damaged row. None of them is
 * coerced to 0, which would import the cookie as a session cookie. Chrome writes 0 for a session cookie, never a negative time.
 */
function chromeExpiry(value: unknown, hostKey: unknown): bigint {
	if (typeof value === 'bigint' && value >= 0n) return value;
	throw new Error(`Chrome cookie expires_utc ${JSON.stringify(String(value))} for ${String(hostKey)} is not a non-negative integer`);
}

const SOURCE_SCHEME: Record<number, 'http' | 'https' | null> = { 0: null, 1: 'http', 2: 'https' };
const DEFAULT_PORT = { http: 80, https: 443 } as const;

/**
 * The origin Electron records as the cookie's source. Chrome keys a cookie by
 * its source scheme and port too, so an https-set cookie without Secure must
 * not become an http one, and a port-bound one keeps its port; a value outside
 * Chrome's enums is a changed format and fails.
 */
function sourceOrigin(row: ChromeCookieRow, secure: boolean, host: string): string {
	const recorded = SOURCE_SCHEME[row.source_scheme];
	if (recorded === undefined) throw new Error(`unknown Chrome source_scheme ${row.source_scheme} for ${row.host_key}`);
	if (secure && recorded === 'http') throw new Error(`Chrome cookie for ${row.host_key} is Secure but was set over http`);
	// Unset predates the column: Chrome then treats Secure as the scheme.
	const scheme = recorded ?? (secure ? 'https' : 'http');
	const port = row.source_port;
	if (port === -1 || port === DEFAULT_PORT[scheme]) return `${scheme}://${host}`;
	if (!Number.isInteger(port) || port < 1 || port > 65535) {
		throw new Error(`Chrome cookie source_port ${port} for ${row.host_key} is not a port`);
	}
	return `${scheme}://${host}:${port}`;
}

export function toImportedCookie(row: ChromeCookieRow, value: string, nowUnix: number): ImportedCookie | null {
	// An unknown SameSite value means Chrome changed its format; guessing
	// 'unspecified' could widen when the cookie is sent.
	const sameSite = SAME_SITE[row.samesite];
	if (sameSite === undefined) throw new Error(`unknown Chrome SameSite value ${row.samesite} for ${row.host_key}`);
	// A flag other than 0 or 1, or a path that is not absolute, is a damaged or
	// changed row: coercing it could downgrade the cookie to plain http or widen
	// its path.
	const secure = chromeFlag(row.is_secure, 'is_secure', row.host_key);
	const httpOnly = chromeFlag(row.is_httponly, 'is_httponly', row.host_key);
	if (typeof row.path !== 'string' || !row.path.startsWith('/')) {
		throw new Error(`Chrome cookie path ${JSON.stringify(row.path)} for ${row.host_key} is not an absolute path`);
	}
	const hostOnly = !row.host_key.startsWith('.');
	const host = row.host_key.replace(/^\./, '');
	// Checked before the expiry skip, so an expired row with a bad scheme or
	// port still fails the import.
	const origin = sourceOrigin(row, secure, host);
	const expires = chromeTimeToUnix(row.expires_utc);
	if (expires !== undefined && expires <= nowUnix) return null;
	const cookie: ImportedCookie = {
		url: `${origin}${row.path}`,
		name: row.name,
		value,
		path: row.path,
		secure,
		httpOnly,
		sameSite
	};
	// A host-only cookie must not get a domain, or Chromium widens it to subdomains.
	if (!hostOnly) cookie.domain = row.host_key;
	if (expires !== undefined) cookie.expirationDate = expires;
	return cookie;
}

export interface ReadResult {
	cookies: ImportedCookie[];
	sites: string[];
	skipped: number;
}

const SNAPSHOT_ATTEMPTS = 5;

function fileSignature(file: string): string {
	try {
		const st = fs.statSync(file, { bigint: true });
		return `${st.size}:${st.mtimeNs}:${st.ino}`;
	} catch (error) {
		// Only a missing file is absent. An unreadable -wal or -shm must abort
		// the snapshot, or the copy silently misses recent cookies.
		if ((error as NodeJS.ErrnoException).code === 'ENOENT') return 'absent';
		throw error;
	}
}

/**
 * Copy a live SQLite database (main file, -wal, -shm) so the copy is one
 * consistent state. Chrome keeps its Cookies database locked and may
 * checkpoint or append to the WAL between the three copies, so the copy is
 * kept only when no source file changed while it was taken; otherwise it is
 * retaken, and a database that never holds still fails loudly. `copy` is the
 * file copier (tests pass one that writes to the source mid-snapshot).
 */
export function snapshotSqlite(
	source: string,
	dest: string,
	copy: (from: string, to: string) => void = (from, to) => fs.copyFileSync(from, to)
): void {
	const files = ['', '-wal', '-shm'];
	for (let attempt = 0; attempt < SNAPSHOT_ATTEMPTS; attempt++) {
		const before = files.map((suffix) => fileSignature(source + suffix));
		files.forEach((suffix, i) => {
			fs.rmSync(dest + suffix, { force: true });
			if (suffix !== '' && before[i] === 'absent') return;
			try {
				copy(source + suffix, dest + suffix);
			} catch (error) {
				// A -wal or -shm that a checkpoint removed mid-copy is Chrome
				// writing, so the signature check below retakes the copy. Any other failure, or a missing main file, is real.
				if (suffix === '' || (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
			}
		});
		const after = files.map((suffix) => fileSignature(source + suffix));
		if (before.every((sig, i) => sig === after[i])) return;
	}
	throw new Error('Chrome kept writing its cookie database while it was copied; quit Chrome and try again.');
}

/**
 * Read and decrypt the listed sites' cookies from a Chrome Cookies database.
 * The file is snapshotted first: Chrome holds it open and may be writing.
 */
export function readChromeLoginCookies(
	cookiesDb: string,
	key: Buffer,
	opts: { sites?: readonly string[]; nowUnix?: number } = {}
): ReadResult {
	const sites = opts.sites ?? LOGIN_SITES;
	const nowUnix = opts.nowUnix ?? Date.now() / 1000;
	const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'opendj-chrome-'));
	const copy = path.join(scratch, 'Cookies');
	try {
		snapshotSqlite(cookiesDb, copy);
		// node:sqlite ships with Node 22.5+ (Electron 44 runs Node 24); loaded
		// lazily so the shell starts on a runtime that lacks it.
		// eslint-disable-next-line @typescript-eslint/no-require-imports
		const { DatabaseSync } = require('node:sqlite') as typeof import('node:sqlite');
		const db = new DatabaseSync(copy, { readOnly: true });
		try {
			// A torn snapshot must fail, not quietly drop rows.
			const check = db.prepare('PRAGMA quick_check').get() as { quick_check?: string } | undefined;
			if (check?.quick_check !== 'ok') {
				throw new Error(`Chrome's cookie database copy is inconsistent (${String(check?.quick_check)}); quit Chrome and try again.`);
			}
			const meta = db.prepare("SELECT value FROM meta WHERE key = 'version'").get() as { value?: string } | undefined;
			// The version decides whether values carry the DB-24 host digest, so a
			// missing or malformed one fails instead of skipping that check.
			// Only a plain decimal counts: Number('') and Number('  ') are 0.
			const rawVersion = meta?.value;
			const dbVersion = Number(rawVersion);
			if ((typeof rawVersion !== 'string' && typeof rawVersion !== 'number') || !/^[0-9]+$/.test(String(rawVersion))) {
				throw new Error(`Chrome's cookie database has no readable version (${String(meta?.value)}); nothing was imported.`);
			}
			// Only the listed sites' rows ever leave SQLite: the filter is in the
			// query, so no other site's cookie is read into this process.
			const { where, params } = loginSiteWhere(sites);
			// A partitioned (CHIPS) cookie is keyed by its top-frame site, which
			// Electron's cookies.set cannot express: imported, it would widen to an
			// ordinary cookie and could overwrite the real login cookie of the same
			// name. Chrome databases older than partitioning have no such rows.
			const columns = db.prepare('PRAGMA table_info(cookies)').all() as { name?: string }[];
			const has = (column: string): boolean => columns.some((c) => c.name === column);
			// Selected rather than filtered in SQL, so a partitioned row still
			// meets the both-values check below before it is dropped.
			const partition = has('top_frame_site_key') ? 'top_frame_site_key' : "'' AS top_frame_site_key";
			// A database from before Chrome recorded a cookie's source reads as
			// Chrome's own values for that case: scheme unset, port unspecified.
			const sourceScheme = has('source_scheme') ? 'source_scheme' : '0 AS source_scheme';
			const sourcePort = has('source_port') ? 'source_port' : '-1 AS source_port';
			const select = db.prepare(
				`SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite, ${sourceScheme}, ${sourcePort}, ${partition} FROM cookies WHERE (${where})`
			);
			// Chrome's microsecond timestamps pass 2^53; read integers as BigInt.
			select.setReadBigInts(true);
			const rows = (select.all(...params) as unknown as ChromeCookieRow[]).map((row) => ({
				...row,
				is_secure: chromeInt(row.is_secure, 'is_secure', row.host_key),
				is_httponly: chromeInt(row.is_httponly, 'is_httponly', row.host_key),
				samesite: chromeInt(row.samesite, 'samesite', row.host_key),
				source_scheme: chromeInt(row.source_scheme, 'source_scheme', row.host_key),
				source_port: chromeInt(row.source_port, 'source_port', row.host_key),
				expires_utc: chromeExpiry(row.expires_utc, row.host_key)
			}));
			const cookies: ImportedCookie[] = [];
			const found = new Set<string>();
			let skipped = 0;
			// A v10 value that will not decrypt means the wrong (or stale) Keychain
			// key or a damaged row; either way the import is refused rather than
			// reported as a success missing a sign-in cookie.
			// Only two rows are skipped: one bound to another host, which Chrome
			// itself drops, and one in a scheme this reader does not know. A wrong
			// key passes the padding check about once in 256 tries and then fails
			// the host check, so a host-check skip counts only once some other
			// value has verified the key.
			let failed = 0;
			let hostMismatched = 0;
			let verified = 0;
			for (const row of rows) {
				const site = hostIsLoginSite(row.host_key, sites);
				if (site === null) continue;
				let value = row.value;
				// Chrome writes a value or its ciphertext, never both: a row with
				// both is damaged, and its plaintext cannot stand in for a
				// ciphertext that might not decrypt. This runs
				// before every skip, so no skippable format can carry it past.
				if (value !== '' && row.encrypted_value.length > 0) {
					throw new Error(`Chrome cookie ${row.name} for ${row.host_key} has both a plain and an encrypted value; nothing was imported.`);
				}
				if (typeof row.top_frame_site_key !== 'string') {
					throw new Error(`Chrome cookie ${row.name} for ${row.host_key} has an unreadable partition key; nothing was imported.`);
				}
				if (row.top_frame_site_key !== '') continue; // partitioned (CHIPS): never imported
				if (SAME_SITE[row.samesite] === undefined) {
					skipped += 1;
					continue;
				}
				if (row.encrypted_value.length > 0) {
					const encrypted = Buffer.from(row.encrypted_value);
					if (encrypted.subarray(0, 3).toString('latin1') !== 'v10') {
						skipped += 1;
						continue;
					}
					// Before DB version 24 a value carries no host digest, so a wrong
					// key that happens to pass the padding and UTF-8 checks would
					// import garbage as verified. Without a deterministic check the
					// import is refused.
					if (dbVersion < 24) {
						throw new Error(
							`Chrome's cookie database is version ${dbVersion}, from before Chrome bound each cookie to its site, so Open DJ cannot verify the "Chrome Safe Storage" key; open Chrome once to upgrade it, then import again. Nothing was imported.`
						);
					}
					try {
						value = decryptChromeValue(encrypted, key, row.host_key, dbVersion);
						verified += 1;
					} catch (error) {
						if (error instanceof ChromeHostCheckError) hostMismatched += 1;
						else failed += 1;
						skipped += 1;
						continue;
					}
				}
				const cookie = toImportedCookie(row, value, nowUnix);
				if (cookie === null) continue;
				cookies.push(cookie);
				found.add(site);
			}
			if (failed > 0) {
				throw new Error(
					`${failed} of Chrome's encrypted cookies could not be decrypted with the "Chrome Safe Storage" key from the Keychain; nothing was imported.`
				);
			}
			if (hostMismatched > 0 && verified === 0) {
				throw new Error(
					`None of Chrome's encrypted cookies verified the "Chrome Safe Storage" key from the Keychain (${hostMismatched} failed the host check); nothing was imported.`
				);
			}
			return { cookies, sites: [...found].sort(), skipped };
		} finally {
			db.close();
		}
	} finally {
		fs.rmSync(scratch, { recursive: true, force: true });
	}
}

export type ChromeProfileLookup = { kind: 'found'; cookies: string } | { kind: 'refused'; reason: string };

/**
 * The Cookies database of the Chrome profile the user last used on macOS.
 * The profile comes only from Chrome's own "Local State": when that file is
 * missing, unreadable or does not name a profile, the import is refused rather
 * than guessing "Default", which on a multi-profile Mac could copy another
 * profile's sign-ins.
 */
export function chromeProfileCookies(home: string = os.homedir()): ChromeProfileLookup {
	const root = path.join(home, 'Library', 'Application Support', 'Google', 'Chrome');
	const localState = path.join(root, 'Local State');
	let text: string;
	try {
		text = fs.readFileSync(localState, 'utf8');
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		return {
			kind: 'refused',
			reason:
				code === 'ENOENT'
					? 'No Google Chrome profile was found on this Mac.'
					: `Chrome's profile list could not be read (${code ?? String(error)}), so Open DJ cannot tell which profile to import from.`
		};
	}
	const damaged: ChromeProfileLookup = {
		kind: 'refused',
		reason: "Chrome's profile list is damaged, so Open DJ cannot tell which profile to import from."
	};
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch {
		return damaged;
	}
	// Every level is a plain record, and the profile list names the profile:
	// an array or a stray last_used could select a pseudo-profile and import
	// the wrong sign-ins.
	const record = (value: unknown): Record<string, unknown> | null =>
		value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
	const profiles = record(record(parsed)?.profile);
	const infoCache = record(profiles?.info_cache);
	if (profiles === null || infoCache === null) return damaged;
	const listed = Object.keys(infoCache);
	let lastUsed = profiles.last_used;
	// No last-used entry but exactly one profile listed: that is not a guess.
	if (lastUsed === undefined && listed.length === 1) lastUsed = listed[0];
	if (typeof lastUsed !== 'string' || !/^[\w ]+$/.test(lastUsed)) {
		return { kind: 'refused', reason: 'Chrome does not say which profile was used last, so Open DJ cannot tell which one to import from.' };
	}
	if (!Object.hasOwn(infoCache, lastUsed)) {
		return { kind: 'refused', reason: `Chrome's last-used profile "${lastUsed}" is not in its profile list, so Open DJ cannot trust it.` };
	}
	// Only ENOENT means "not here": existsSync also answers false for a file
	// it cannot inspect, which would hide an unreadable modern database or
	// fall back to a stale legacy one.
	for (const candidate of [path.join(root, lastUsed, 'Network', 'Cookies'), path.join(root, lastUsed, 'Cookies')]) {
		try {
			fs.statSync(candidate);
		} catch (error) {
			const code = (error as NodeJS.ErrnoException).code;
			if (code === 'ENOENT') continue;
			return { kind: 'refused', reason: `Chrome's cookie database ${candidate} could not be checked (${code ?? String(error)}); nothing was imported.` };
		}
		return { kind: 'found', cookies: candidate };
	}
	return { kind: 'refused', reason: `Chrome's last-used profile "${lastUsed}" has no cookie database.` };
}

/** The Keychain password Chrome encrypts cookies with. macOS asks the user first. */
export function chromeSafeStoragePassword(): Promise<string> {
	return new Promise((resolve, reject) => {
		execFile(
			'/usr/bin/security',
			['find-generic-password', '-w', '-s', 'Chrome Safe Storage', '-a', 'Chrome'],
			{ timeout: 120_000 },
			(error, stdout) => {
				if (error) reject(new Error('Keychain access to "Chrome Safe Storage" was denied or not found'));
				else resolve(stdout.trim());
			}
		);
	});
}

export type ImportOutcome =
	| { kind: 'imported'; browser: string; cookies: number; sites: string[] }
	| { kind: 'refused'; reason: string };

/**
 * Write the read cookies into the side player's cookie store. A cookie the
 * store rejects makes the whole import a refusal that names how many failed,
 * never a success that hides it. Cookies already written
 * stay written; importing again retries the rest.
 */
export async function storeImportedCookies(
	store: { set: (cookie: ImportedCookie) => Promise<void>; flushStore: () => Promise<void> },
	result: ReadResult
): Promise<ImportOutcome> {
	let set = 0;
	const refused: string[] = [];
	for (const cookie of result.cookies) {
		try {
			await store.set(cookie);
			set += 1;
		} catch (error) {
			refused.push(`${new URL(cookie.url).hostname} ${cookie.name}: ${error instanceof Error ? error.message : String(error)}`);
		}
	}
	await store.flushStore();
	if (refused.length > 0) {
		return {
			kind: 'refused',
			reason: `Open DJ refused ${refused.length} of ${result.cookies.length} Chrome cookies (${refused.slice(0, 3).join('; ')}${refused.length > 3 ? '; ...' : ''}); sign-in may be incomplete.`
		};
	}
	return { kind: 'imported', browser: 'Google Chrome', cookies: set, sites: result.sites };
}

export function unsupportedPlatformReason(platform: NodeJS.Platform): string | null {
	if (platform === 'darwin') return null;
	if (platform === 'win32') {
		return 'Chrome 127 and later on Windows encrypt sign-ins so that only Chrome can read them (Google, Jul 2024), so they cannot be imported. Sign in once in the side player instead.';
	}
	return 'Importing Chrome sign-ins is only supported on macOS so far. Sign in once in the side player instead.';
}
