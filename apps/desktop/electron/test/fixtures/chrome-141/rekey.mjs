// Re-keys a Chromium-written Cookies database for test/side-player.test.ts
// (SPOT-6555c), after capture.mjs:
//
//   CHROMIUM_BASIC_STORE_PASSWORD=<password> \
//     node rekey.mjs <profile-dir>/Default/Cookies ./Cookies
//
// Chromium encrypts with a key it ships for its Linux basic store. Research
// rule R7 forbids publishing a key that unlocks encrypted data, even one an
// open project ships, so that password is never written in this repository:
// supply it from Chromium's own source when re-capturing. This script
// decrypts each value with it and seals the same plaintext (Chromium's
// host digest plus the value) under FIXTURE_PASSWORD, derived the macOS way,
// which opens nothing but these fixture values. Every other byte of every
// row, and the schema, stays as Chromium wrote it. Then update MANIFEST.json
// and FIXTURE_SHA256 in test/side-player.test.ts to the new digest.
import { createCipheriv, createDecipheriv, createHash, pbkdf2Sync } from 'node:crypto';
import fs from 'node:fs';
import { DatabaseSync } from 'node:sqlite';

export const FIXTURE_PASSWORD = 'open-dj-chrome-fixture';
const IV = Buffer.alloc(16, 0x20);

const [source, target] = process.argv.slice(2);
if (!source || !target) throw new Error('usage: rekey.mjs <chromium Cookies> <fixture Cookies>');
const password = process.env.CHROMIUM_BASIC_STORE_PASSWORD;
if (!password) throw new Error('CHROMIUM_BASIC_STORE_PASSWORD is required; it is never stored here (rule R7)');
const from = pbkdf2Sync(password, 'saltysalt', 1, 16, 'sha1');
const to = pbkdf2Sync(FIXTURE_PASSWORD, 'saltysalt', 1003, 16, 'sha1');

fs.copyFileSync(source, target);
const db = new DatabaseSync(target);
const rows = db.prepare("SELECT rowid, host_key, encrypted_value FROM cookies WHERE length(encrypted_value) > 0").all();
const update = db.prepare('UPDATE cookies SET encrypted_value = ? WHERE rowid = ?');
for (const row of rows) {
	const sealed = Buffer.from(row.encrypted_value);
	if (sealed.subarray(0, 3).toString('latin1') !== 'v10') throw new Error(`row ${row.rowid}: not a v10 value`);
	const decipher = createDecipheriv('aes-128-cbc', from, IV);
	const plain = Buffer.concat([decipher.update(sealed.subarray(3)), decipher.final()]);
	// The wrong source password must fail here, not seal garbage.
	const digest = createHash('sha256').update(row.host_key).digest();
	if (!plain.subarray(0, 32).equals(digest)) throw new Error(`row ${row.rowid}: host digest mismatch; wrong source password?`);
	const cipher = createCipheriv('aes-128-cbc', to, IV);
	update.run(Buffer.concat([Buffer.from('v10'), cipher.update(plain), cipher.final()]), row.rowid);
}
db.close();
console.log(`re-keyed ${rows.length} values; sha256 ${createHash('sha256').update(fs.readFileSync(target)).digest('hex')}`);
