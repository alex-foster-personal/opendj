/**
 * Recorded daemon responses, served to Storybook contracts through MSW.
 *
 * Every file under ./recorded/ was captured from a real daemon by
 * scripts/record-sandbox-fixtures.mjs (entries in ./recordings.json name the
 * fixture library it was serving). Re-record rather than hand-edit: a
 * recorded body that has drifted from the API is caught by re-running the
 * recorder, while a hand-typed one is only ever as right as its author.
 *
 * Sandbox only. App code must never import from src/sandbox/ - the guard in
 * tests/unit/storybook-stories.test.mjs fails if it does.
 */
import type { HttpHandler } from 'msw';

import lyricsBuilderText from '../../tests/e2e/support/lyrics_words_fixture.py?raw';
import checksums from './recorded-checksums.json';
import manifestText from './recorded-manifest.json?raw';
import recordings from './recordings.json';
import { checkRecordedSet, replayRecording, type RecordedChecksums, type Recording } from './replay';
import { sha256Hex } from './sha256';

// The raw text, not the parsed module, so the loader can hash the exact bytes
// the pins name.
const RAW = import.meta.glob<string>('./recorded/*.json', { eager: true, query: '?raw', import: 'default' });
// Every capture must be pinned, match its pinned bytes, and have been
// recorded against the committed manifest and the builder as checked out; an
// unpinned, edited or hand-added file, one recordings.json no longer lists,
// or one a builder change left stale,
// fails before any replay.
const FILES = checkRecordedSet(RAW, checksums as RecordedChecksums, {
	manifest_sha256: sha256Hex(manifestText),
	builders: { 'tests/e2e/support/lyrics_words_fixture.py': sha256Hex(lyricsBuilderText) },
	listed: recordings.recordings.map((r) => r.name)
});

const BY_NAME = new Map<string, Recording>(
	Object.values(FILES).map((recording) => [recording.name, recording])
);

function recording(name: string): Recording {
	const found = BY_NAME.get(name);
	if (found === undefined) {
		throw new Error(
			`no recorded fixture '${name}' (have: ${[...BY_NAME.keys()].join(', ')}); add it to src/sandbox/recordings.json and run scripts/record-sandbox-fixtures.mjs`
		);
	}
	return found;
}

/** An MSW handler replaying one recording at its recorded path, method and
 * query, with its recorded status and content type (see ./replay.ts). */
export function replay(name: string): HttpHandler {
	return replayRecording(recording(name));
}

/** The track id a recording was captured at, read from its request path, so
 * a story follows the fixture library through a re-record instead of
 * pinning ids a fresh build would not reproduce. */
export function recordedTrackId(name: string): string {
	const path = recording(name).request.path;
	const id = /\/tracks\/([0-9a-f]{40})\//.exec(path)?.[1];
	if (id === undefined) throw new Error(`recorded fixture '${name}' (${path}) names no track`);
	return id;
}
