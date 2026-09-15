/**
 * Requirements: LIBUX-07, LIBUX-13.
 *
 * TrackTable's CloudSync cell decisions run through the production helper.
 * The inputs are the independent local-file and durable-remote-copy facts
 * that production publishes; no transfer percentage or state is faked.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let trackCloudState;

before(async () => {
	trackCloudState = await loadTypeScriptModule('src/lib/components/rb/browser/track-cloud-state.ts');
});

function view(overrides = {}) {
	return trackCloudState.trackCloudView({
		fileExists: true,
		isStreaming: false,
		hasRemoteCopy: false,
		transfer: null,
		...overrides
	});
}

test('the three requested CloudSync states stay distinct', () => {
	assert.deepEqual(view(), {
		kind: 'not-on-cloud',
		title: 'Not on CloudSync; audio is available only on this machine.',
		showIcon: true,
		transfer: null
	});
	assert.deepEqual(view({ hasRemoteCopy: true, fileExists: false }), {
		kind: 'on-cloud-not-local',
		title: 'On CloudSync but not stored locally on this machine.',
		showIcon: true,
		transfer: null
	});
	assert.deepEqual(view({ hasRemoteCopy: true }), {
		kind: 'on-cloud-and-local',
		title: 'On CloudSync and stored locally on this machine.',
		showIcon: true,
		transfer: null
	});
});

test('streaming stays distinct and real byte counts drive horizontal progress', () => {
	assert.equal(
		view({ isStreaming: true, hasRemoteCopy: true, fileExists: false }).kind,
		'streaming'
	);
	const upload = view({
		transfer: { direction: 'upload', bytesTransferred: 4, bytesTotal: 10 }
	});
	assert.equal(upload.kind, 'not-on-cloud');
	assert.deepEqual(upload.transfer, {
		direction: 'upload',
		percent: 40,
		label: 'Uploading to CloudSync: 40% (4 of 10 bytes).'
	});
	assert.match(upload.title, /Uploading to CloudSync: 40%/);

	const download = view({
		hasRemoteCopy: true,
		fileExists: false,
		transfer: { direction: 'download', bytesTransferred: 6, bytesTotal: 10 }
	});
	assert.equal(download.kind, 'on-cloud-not-local');
	assert.equal(download.transfer.percent, 60);
	assert.match(download.transfer.label, /Downloading from CloudSync/);
});

test('indeterminate progress is reserved for a genuinely unavailable total', () => {
	const loading = view({
		hasRemoteCopy: true,
		fileExists: false,
		transfer: { direction: 'download', bytesTransferred: 32, bytesTotal: null }
	});
	assert.equal(loading.transfer.percent, null);
	assert.match(loading.transfer.label, /exact percentage unavailable/i);
});

test('TrackTable renders crossed, red, and plain cloud states with explanatory hover', async () => {
	const source = await readFile('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');
	const cloudCell = source.slice(source.indexOf('<td class="c-cloud">'), source.indexOf('<td class="c-order"'));
	// 00a092243 hoisted the {@const cloudView} out of the <td> to the top of
	// the row's {#each} block (Svelte rejects {@const} inside an element).
	const viewAt = source.indexOf('{@const cloudView = trackCloudView({');
	assert.ok(viewAt >= 0, 'the per-row cloudView binding moved');
	const rowCloudView = source.slice(viewAt, source.indexOf('<tr', viewAt));
	assert.match(rowCloudView, /hasRemoteCopy: row\.has_remote_copy === true/);
	assert.match(rowCloudView, /bytesTransferred: row\.cloud_transfer\.bytes_transferred/);
	assert.match(cloudCell, /class:not-on-cloud=/);
	assert.match(cloudCell, /class:on-cloud-not-local=/);
	assert.match(cloudCell, /class:on-cloud-and-local=/);
	assert.match(cloudCell, /title=\{cloudView\.title\}/);
	assert.doesNotMatch(cloudCell, /audioPrefetchStatus\(row\.stable_id\)/);
	assert.match(cloudCell, /role="progressbar"/);
	assert.match(cloudCell, /aria-valuenow=\{cloudView\.transfer\.percent \?\? undefined\}/);
	assert.match(cloudCell, /aria-valuetext=\{cloudView\.transfer\.label\}/);
	assert.match(cloudCell, /class="cloud-transfer-track"/);
	assert.match(cloudCell, /`width:\$\{cloudView\.transfer\.percent\}%`/);
});

test('the utility column reserves enough width for full-state cloud icons', async () => {
	const { compactUtilityWidths } = await loadTypeScriptModule('src/lib/rb/library-column-widths.ts');
	assert.equal(compactUtilityWidths(1).cloud, 22);
});
