import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * r3920201245 (Codex, PR #890): a real captured fixture read with no version,
 * manifest, or checksum check lets a silent edit update both the capture and
 * its consuming tests' expectations together, so the suite still passes
 * against altered evidence. Mirrors prefs-golden-blob.test.mjs's existing
 * verifiedCapture pattern for fixtures shared by more than one test file.
 *
 * `fixturesDir` and `manifestPath` are plain filesystem paths - resolve them
 * with `fileURLToPath(new URL('./fixtures/...', import.meta.url))` first, the
 * same way prefs-golden-blob.test.mjs already does.
 */
const SUPPORTED_MANIFEST_VERSION = 1;

export function verifiedFixtureText(fixturesDir, manifestPath, name) {
	const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
	assert.equal(
		manifest.version,
		SUPPORTED_MANIFEST_VERSION,
		`${manifestPath} declares version ${manifest.version}, this helper supports ` +
			`${SUPPORTED_MANIFEST_VERSION}. Failing closed rather than reading a format whose ` +
			'meaning is not known.'
	);
	const entry = manifest.files[name];
	assert.ok(entry, `${name} is not listed in ${manifestPath}`);
	const bytes = readFileSync(join(fixturesDir, name));
	assert.equal(
		bytes.length,
		entry.bytes,
		`${name} is ${bytes.length} bytes, manifest says ${entry.bytes}. This is a byte-` +
			'for-byte capture; a length change means the file was rewritten.'
	);
	assert.equal(
		createHash('sha256').update(bytes).digest('hex'),
		entry.sha256,
		`${name} does not match its manifest checksum. Captures are IMMUTABLE: ` +
			'regenerating one to make a test pass deletes the only evidence of what was ' +
			'really captured.'
	);
	return bytes.toString('utf8');
}
