/**
 * How one recording is replayed through MSW. Kept apart from ./recorded.ts
 * (which loads ./recorded/ with Vite's import.meta.glob) so the matching and
 * response rules run under plain node in tests/unit/sandbox-replay.test.mjs.
 *
 * A replay honors the WHOLE recorded request, not just its path: the query
 * the daemon answered is part of the contract, so a component that drops or
 * adds a parameter (e.g. `include=lines`) gets a loud 500 naming expected vs
 * actual, never the recorded body it no longer earns. The response carries
 * the recorded status AND content type, body or no body.
 *
 * Sandbox only. App code must never import from src/sandbox/.
 */
import { http, HttpResponse, type HttpHandler } from 'msw';

import { sha256Hex } from './sha256.ts';

export interface Recording {
	name: string;
	recorded_at: string;
	library: string;
	/** Hashes of the builder manifest and source the recorder verified the
	 * daemon against. */
	provenance: { manifest_sha256: string; library_sha256: string };
	request: {
		method: 'GET' | 'HEAD' | 'PUT' | 'POST';
		path: string;
		query: Record<string, string>;
	};
	response: { status: number; content_type: string | null; body: unknown };
}

export interface RecordedChecksums {
	files: Record<string, { sha256: string }>;
}

/** What the captures must have been recorded against: the committed
 * manifest's sha256, and each fixture builder's sha256 as checked out, keyed
 * by the path a recording names in `library`. */
export interface ExpectedProvenance {
	manifest_sha256: string;
	builders: Record<string, string>;
	/** Every recording ./recordings.json names; a capture outside it is stale. */
	listed: string[];
}

/** The recordings in `raw` (file path -> file text), parsed. Throws unless
 * the files and the checksum pins name the same set, every file's bytes match
 * its pinned sha256, and every file was recorded against the committed
 * manifest and the builder as checked out, so a hand edit, or a builder or
 * manifest change without a re-record, fails before any story replays it. */
export function checkRecordedSet(
	raw: Record<string, string>,
	pins: RecordedChecksums,
	expected: ExpectedProvenance
): Record<string, Recording> {
	const names = Object.keys(raw).map((path) => path.replace(/^.*\//, ''));
	const unpinned = names.filter((name) => !(name in pins.files));
	const missing = Object.keys(pins.files).filter((name) => !names.includes(name));
	if (unpinned.length > 0 || missing.length > 0) {
		throw new Error(
			`recorded fixtures and recorded-checksums.json disagree: unpinned ${JSON.stringify(unpinned)}, missing ${JSON.stringify(missing)}; re-record with scripts/record-sandbox-fixtures.mjs`
		);
	}
	const listed = expected.listed.map((name) => `${name}.json`);
	const stale = names.filter((name) => !listed.includes(name));
	const unrecorded = listed.filter((name) => !names.includes(name));
	if (stale.length > 0 || unrecorded.length > 0) {
		throw new Error(
			`recorded fixtures and recordings.json disagree: not listed ${JSON.stringify(stale)}, not recorded ${JSON.stringify(unrecorded)}; re-record with scripts/record-sandbox-fixtures.mjs`
		);
	}
	const files: Record<string, Recording> = {};
	for (const [path, text] of Object.entries(raw)) {
		const name = path.replace(/^.*\//, '');
		const sha = sha256Hex(text);
		if (sha !== pins.files[name].sha256) {
			throw new Error(`${name} has sha256 ${sha}, not its pinned ${pins.files[name].sha256}; it changed without the recorder, so re-record it with scripts/record-sandbox-fixtures.mjs`);
		}
		const rec = JSON.parse(text) as Recording;
		const hashed = typeof rec.provenance?.manifest_sha256 === 'string' && typeof rec.provenance?.library_sha256 === 'string';
		if (!hashed) {
			throw new Error(`${name} carries no manifest and builder hashes; re-record it with scripts/record-sandbox-fixtures.mjs`);
		}
		if (rec.provenance.manifest_sha256 !== expected.manifest_sha256) {
			throw new Error(`${name} was recorded against manifest ${rec.provenance.manifest_sha256}, not the committed one (${expected.manifest_sha256}); re-record the set`);
		}
		const builder = expected.builders[rec.library];
		if (builder === undefined) {
			throw new Error(`${name} names builder ${rec.library}, which the loader does not know; add it to ./recorded.ts`);
		}
		if (rec.provenance.library_sha256 !== builder) {
			throw new Error(`${name} predates ${rec.library} as checked out (recorded ${rec.provenance.library_sha256}, now ${builder}); re-record the set`);
		}
		files[path] = rec;
	}
	return files;
}

/** Null when `actual` carries exactly the recorded query, else a message
 * naming expected vs actual. Order-insensitive; a repeated key never matches
 * a recording, which stores one value per key. */
export function queryMismatch(
	expected: Record<string, string>,
	actual: URLSearchParams
): string | null {
	const got: Record<string, string | string[]> = {};
	for (const key of new Set(actual.keys())) {
		const values = actual.getAll(key);
		got[key] = values.length === 1 ? values[0] : values;
	}
	const keys = new Set([...Object.keys(expected), ...Object.keys(got)]);
	for (const key of keys) {
		if (got[key] !== expected[key]) {
			return `query mismatch: expected ${JSON.stringify(expected)}, got ${JSON.stringify(got)}`;
		}
	}
	return null;
}

// Query drift is a story failure, not just a 500: a component that catches
// server errors would render its error state and could still pass. The
// Storybook previews drain this list in afterEach (.storybook/unhandled-api.ts).
const drift: string[] = [];

/** Every replay mismatch since the last call, oldest first; clears the list. */
export function takeReplayDrift(): string[] {
	return drift.splice(0);
}

/** An MSW handler replaying `rec` at its recorded method, path and query. */
export function replayRecording(rec: Recording): HttpHandler {
	const method = rec.request.method.toLowerCase() as 'get' | 'head' | 'put' | 'post';
	return http[method](rec.request.path, ({ request }) => {
		const mismatch = queryMismatch(rec.request.query, new URL(request.url).searchParams);
		if (mismatch !== null) {
			const message = `sandbox replay '${rec.name}' (${rec.request.method} ${rec.request.path}): ${mismatch}`;
			console.error(message);
			drift.push(message);
			return HttpResponse.json(
				{ detail: { code: 'sandbox_replay_mismatch', message } },
				{ status: 500 }
			);
		}
		const headers = new Headers();
		if (rec.response.content_type !== null) {
			headers.set('content-type', rec.response.content_type);
		}
		const body = rec.response.body === null ? null : JSON.stringify(rec.response.body);
		return new HttpResponse(body, { status: rec.response.status, headers });
	});
}
