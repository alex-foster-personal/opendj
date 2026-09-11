import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { basename } from 'node:path';
import { fileURLToPath } from 'node:url';

const MANIFEST_SCHEMA_VERSION = 1;

/** Read a canonical JSON capture only after its immutable manifest matches.
 *
 * Version, byte count, and SHA-256 are checked before JSON.parse so an
 * accidental fixture edit cannot become the test's new authoritative input.
 */
export function loadVerifiedJsonCapture(captureUrl, manifestUrl) {
	const captureName = basename(fileURLToPath(captureUrl));
	const manifest = JSON.parse(readFileSync(manifestUrl, 'utf8'));
	if (manifest.schema_version !== MANIFEST_SCHEMA_VERSION) {
		throw new Error(
			`${captureName}: manifest schema must be ${MANIFEST_SCHEMA_VERSION}, got ${String(manifest.schema_version)}`
		);
	}
	const entry = manifest.files?.[captureName];
	if (entry === undefined) throw new Error(`${captureName}: manifest has no entry`);
	if (typeof entry.bytes !== 'number' || typeof entry.sha256 !== 'string') {
		throw new Error(`${captureName}: manifest entry must declare bytes and sha256`);
	}
	const raw = readFileSync(captureUrl);
	if (raw.byteLength !== entry.bytes) {
		throw new Error(`${captureName}: byte length drifted from manifest`);
	}
	const actual = createHash('sha256').update(raw).digest('hex');
	if (actual !== entry.sha256) {
		throw new Error(`${captureName}: SHA-256 drifted from manifest; re-capture and regenerate the manifest`);
	}
	return JSON.parse(raw.toString('utf8'));
}
