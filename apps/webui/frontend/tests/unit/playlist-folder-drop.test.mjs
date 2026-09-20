// requirement: LIBUX-16
// [if] upload returns duplicate and new rows [then] stable_ids resolve for playlist add [else stop].

import assert from 'node:assert/strict';
import { before, describe, it, mock } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let folderDrop;

before(async () => {
	folderDrop = await loadTypeScriptModule('src/lib/rb/playlist-folder-drop.ts');
});

describe('batchNameFromFolder', () => {
	it('uses a sanitized folder name when it matches BATCH_RE', () => {
		assert.equal(folderDrop.batchNameFromFolder('Agnes Obel'), 'Agnes Obel');
	});

	it('falls back to drop- timestamp for invalid batch names', () => {
		const name = folderDrop.batchNameFromFolder('!!!');
		assert.match(name, /^drop-\d{8}-\d{4}$/);
	});
});

describe('ingestFolderToNewPlaylist', () => {
	it('links duplicates and materializes new files', async () => {
		const calls = [];
		global.fetch = mock.fn(async (url, init) => {
			calls.push({ url: String(url), init });
			if (String(url).endsWith('/api/v1/playlists') && init?.method === 'POST') {
				return new Response(JSON.stringify({ playlist_id: 'pl-1', name: 'Mix', items: [] }), {
					status: 201,
					headers: { etag: '"e1"' }
				});
			}
			if (String(url).endsWith('/api/v1/ingest/upload')) {
				return new Response(
					JSON.stringify({
						batch: 'mix',
						dest_dir: '/tmp/_ingest/mix',
						results: [
							{
								filename: 'dup.mp3',
								staged_path: null,
								skipped_duplicate: true,
								verdict: 'skipped_duplicate',
								duplicate_of: { stable_id: 'dup-id', title: 'Dup', artist: null, method: 'chromaprint', score: 0.99 },
								duration_s: 200,
								fingerprint_method: 'chromaprint'
							},
							{
								filename: 'new.mp3',
								staged_path: '/tmp/_ingest/mix/new.mp3',
								skipped_duplicate: false,
								verdict: 'new',
								duplicate_of: null,
								duration_s: 200,
								fingerprint_method: 'chromaprint'
							}
						]
					}),
					{ status: 200 }
				);
			}
			if (String(url).includes('/materialize')) {
				return new Response(
					JSON.stringify({
						batch: 'mix',
						tracks: [{ relative_path: 'new.mp3', stable_id: 'new-id', inserted: true }]
					}),
					{ status: 200 }
				);
			}
			if (String(url).includes('/items:add')) {
				return new Response(JSON.stringify({ playlist_id: 'pl-1', items: ['dup-id', 'new-id'] }), {
					status: 200,
					headers: { etag: '"e2"' }
				});
			}
			if (String(url).endsWith('/api/v1/ingest/refresh')) {
				return new Response(JSON.stringify({ running: true, phase: 'queued' }), { status: 200 });
			}
			if (String(url).endsWith('/api/v1/ingest/pending')) {
				return new Response(JSON.stringify({ batches: [] }), { status: 200 });
			}
			throw new Error(`unexpected fetch ${url}`);
		});

		const file = new File(['x'], 'new.mp3', { type: 'audio/mpeg' });
		const result = await folderDrop.ingestFolderToNewPlaylist({
			files: [file],
			folderName: 'Mix'
		});
		assert.equal(result.playlistId, 'pl-1');
		assert.equal(result.added, 2);
		assert.equal(result.staged, 1);
		assert.equal(result.skippedDup, 1);
		assert.ok(calls.some((c) => String(c.url).includes('/materialize')));
	});
});
