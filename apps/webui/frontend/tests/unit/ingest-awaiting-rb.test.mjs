// requirement: LIBMX-13
// [if] a detected USB drive is clicked [then] an import action is reachable from that row, not just a yours/not-yours classifier, [else stop].
// [if] a folder containing audio files is dropped onto the ingest surface [then] the files inside it are found and staged, not reported as no audio files, [else stop].
// [if] a batch is staged and awaiting a manual Rekordbox import [then] a persistent UI indicator says so until the user confirms it is done, [else stop].
// [if] a possible-dup match is shown during staging [then] the user can accept or reject it inline, not just read a status line, [else stop].

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

describe('awaiting-Rekordbox chrome (LIBMX-13)', () => {
	it('LibraryJobsChrome has ingest-awaiting-rb chip and confirm', () => {
		const src = readFileSync(
			new URL(
				'../../src/lib/components/rb/library-jobs/LibraryJobsChrome.svelte',
				import.meta.url
			),
			'utf8'
		);
		assert.match(src, /data-testid="ingest-awaiting-rb"/);
		assert.match(src, /data-testid="ingest-awaiting-rb-confirm"/);
		assert.match(src, /confirmIngestPending/);
	});

	it('IngestDropModal has dup accept/reject and defers refresh', () => {
		const src = readFileSync(
			new URL('../../src/lib/components/rb/IngestDropModal.svelte', import.meta.url),
			'utf8'
		);
		assert.match(src, /data-testid="ingest-dup-accept"/);
		assert.match(src, /data-testid="ingest-dup-reject"/);
		assert.match(src, /collectDroppedAudioFiles/);
		assert.match(src, /_maybeStartRefresh/);
		assert.match(src, /await _maybeStartRefresh\(\)/);
		assert.doesNotMatch(
			src,
			/uploadIngestFiles\(files, batch\)[\s\S]{0,200}startIngestRefresh/
		);
	});

	it('IngestDropModal still mentions Rekordbox import', () => {
		const src = readFileSync(
			new URL('../../src/lib/components/rb/IngestDropModal.svelte', import.meta.url),
			'utf8'
		);
		assert.match(src, /Import the folder into[\s\S]*Rekordbox/);
	});
});
