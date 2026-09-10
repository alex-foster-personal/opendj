/**
 * Provenance for a live run's audio inputs.
 *
 * The live rungs read real audio out of a gitignored or operator-supplied
 * directory, because the claim under test is about REAL FLAC and a synthesized
 * tone exercises no decoder edge case. That makes the inputs mutable: the same
 * command can select different files tomorrow (a track added ahead of the
 * others alphabetically, a re-encode in place, a different `Q18_FLAC_DIR`) and
 * still print PASS. A pass whose subject is unknown is not acceptance evidence
 * for anything, and "four .flac files, 3.1 MB each" does not identify audio.
 *
 * So a run VERIFIES what it decoded, by content, against a manifest that was
 * pinned earlier and reviewed. Recording that manifest is a SEPARATE MODE that
 * runs no checks and cannot report acceptance success, because a run allowed
 * to mint its own baseline verifies nothing: whichever four files happened to
 * sort first become the contract, and the very next line is a PASS.
 *
 * Kept in its own module, separate from the runner, so the comparison is
 * reachable from the unit suite: the runner needs two browsers and several
 * gigabytes of audio, which is exactly the reason a defect in this logic would
 * otherwise never be caught by a test.
 */

import { createHash } from 'node:crypto';
import path from 'node:path';

/**
 * Bumped when the RECORDED SHAPE changes, so a stale file is reported as
 * stale rather than being half-read into a comparison that skips fields.
 */
export const MANIFEST_VERSION = 1;

/**
 * @param {ReadonlyArray<{part: string, file: string, bytes: Uint8Array}>} selected
 * @returns {{version: number, parts: Array<{part: string, name: string, bytes: number, sha256: string}>}}
 */
export function buildManifest(selected) {
	return {
		version: MANIFEST_VERSION,
		parts: selected.map(({ part, file, bytes }) => ({
			part,
			name: path.basename(file),
			bytes: bytes.length,
			sha256: createHash('sha256').update(bytes).digest('hex')
		}))
	};
}

/**
 * Every way `recorded` disagrees with `observed`, as human sentences.
 *
 * An empty result has to mean VERIFIED, never "nothing was comparable", so a
 * malformed, empty or truncated recording is a mismatch in its own right
 * rather than a comparison that quietly has nothing to do. That is the whole
 * failure mode this module exists to close: a check that cannot fail reports
 * the same zero as a check that passed.
 *
 * @param {unknown} recorded
 * @param {{version: number, parts: Array<{part: string, name: string, bytes: number, sha256: string}>}} observed
 * @returns {string[]}
 */
export function manifestMismatches(recorded, observed) {
	if (recorded === null || typeof recorded !== 'object' || Array.isArray(recorded)) {
		return [`recorded manifest is ${recorded === null ? 'null' : typeof recorded}, not an object`];
	}
	const problems = [];
	if (recorded.version !== observed.version) {
		problems.push(
			`manifest version ${JSON.stringify(recorded.version)} was recorded, this run emits ${observed.version}`
		);
	}
	if (!Array.isArray(recorded.parts)) {
		problems.push('recorded manifest has no parts array, so it pins nothing');
		return problems;
	}
	const byPart = new Map(recorded.parts.map((entry) => [entry?.part, entry]));
	for (const seen of observed.parts) {
		const was = byPart.get(seen.part);
		byPart.delete(seen.part);
		if (was === undefined) {
			problems.push(`part ${seen.part} is decoded by this run but absent from the manifest`);
			continue;
		}
		// Content first: a rename with identical bytes is the same audio and a
		// re-encode under the same name is not, and only the digest can tell
		// those two apart. The name is reported anyway because it is what an
		// operator can act on.
		if (was.sha256 !== seen.sha256) {
			problems.push(
				`part ${seen.part} is different audio: recorded ${was.name} sha256 ${short(was.sha256)}, this run read ${seen.name} sha256 ${short(seen.sha256)}`
			);
			continue;
		}
		if (was.bytes !== seen.bytes) {
			problems.push(
				`part ${seen.part} matches by digest but not by size (${was.bytes} recorded, ${seen.bytes} read), so one of the two numbers is wrong`
			);
		}
		if (was.name !== seen.name) {
			problems.push(
				`part ${seen.part} is the same audio under a different name (${was.name} recorded, ${seen.name} read)`
			);
		}
	}
	for (const leftover of byPart.keys()) {
		problems.push(`part ${String(leftover)} is pinned by the manifest but was not decoded by this run`);
	}
	return problems;
}

/**
 * What a run is allowed to do with the manifest it just built.
 *
 * A decision function rather than four branches inside the runner: the runner
 * needs two browsers and gigabytes of audio to reach any of this, so branches
 * living there are branches nothing can test. Every outcome except `verified`
 * must stop the run.
 *
 * @param {{recordMode: boolean, manifestExists: boolean, recorded: unknown, observed: object}} state
 * @returns {{action: 'record'|'refuse-overwrite'|'missing'|'mismatch'|'verified', problems: string[]}}
 */
export function manifestGate({ recordMode, manifestExists, recorded, observed }) {
	if (recordMode) {
		// Re-pinning is deliberate or it is not re-pinning. An overwrite that
		// just happens is how a drifted directory becomes the new contract.
		if (manifestExists) {
			return { action: 'refuse-overwrite', problems: manifestMismatches(recorded, observed) };
		}
		return { action: 'record', problems: [] };
	}
	if (!manifestExists) return { action: 'missing', problems: [] };
	const problems = manifestMismatches(recorded, observed);
	return { action: problems.length === 0 ? 'verified' : 'mismatch', problems };
}

function short(digest) {
	return typeof digest === 'string' ? digest.slice(0, 12) : String(digest);
}
