// Port of the tests in src-tauri/src/engine_log.rs.
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { afterEach, test } from 'node:test';

import {
	ENGINE_LOG_MAX_ARCHIVE_COUNT,
	ENGINE_LOG_MAX_BYTES,
	ENGINE_LOG_MIN_FREE_BYTES,
	appendRotated,
	archivePaths,
	archiveTimestamp,
	pruneArchives,
	resetLowDiskState,
	rotateLogIfNeeded,
	setDiskFreeOverride
} from '../src/engine-log';
import { scratchDir } from './helpers';

const HEALTHY_FREE_BYTES = 10 * ENGINE_LOG_MIN_FREE_BYTES;
const DAY_S = 24 * 60 * 60;

afterEach(() => resetLowDiskState());

function setMtime(file: string, secondsAgo: number): void {
	const when = new Date(Date.now() - secondsAgo * 1000);
	fs.utimesSync(file, when, when);
}

test('rotates a full log into a timestamped archive', () => {
	const dir = scratchDir('rotate');
	const log = path.join(dir, 'engine.log');
	fs.writeFileSync(log, 'current');
	setDiskFreeOverride(HEALTHY_FREE_BYTES);
	rotateLogIfNeeded(log, 1);
	assert.equal(fs.existsSync(log), false);
	const archives = archivePaths(log);
	assert.equal(archives.length, 1);
	assert.match(path.basename(archives[0] as string), /^engine\.log\.\d{8}T\d{6}Z$/);
	assert.equal(fs.readFileSync(archives[0] as string, 'utf8'), 'current');
});

test('removes archives older than retention and keeps recent ones', () => {
	const dir = scratchDir('prune-old');
	const log = path.join(dir, 'engine.log');
	const old = path.join(dir, 'engine.log.20260101T000000Z');
	const recent = path.join(dir, 'engine.log.20260901T000000Z');
	fs.writeFileSync(old, 'old');
	fs.writeFileSync(recent, 'recent');
	setMtime(old, 8 * DAY_S);
	setMtime(recent, 60 * 60);
	pruneArchives(log);
	assert.equal(fs.existsSync(old), false);
	assert.equal(fs.existsSync(recent), true);
});

test('keeps at most the max archive count, dropping the oldest', () => {
	const dir = scratchDir('keep-max');
	const log = path.join(dir, 'engine.log');
	for (let index = 0; index < 25; index += 1) {
		const archive = path.join(dir, `engine.log.202609${String(index).padStart(2, '0')}T000000Z`);
		fs.writeFileSync(archive, `archive-${index}`);
		setMtime(archive, 60 * (index + 1));
	}
	fs.writeFileSync(log, 'live');
	setDiskFreeOverride(HEALTHY_FREE_BYTES);
	rotateLogIfNeeded(log, 1);
	const archives = archivePaths(log);
	assert.equal(archives.length, ENGINE_LOG_MAX_ARCHIVE_COUNT);
	// The oldest (index 24, 25 minutes ago) went first; the fresh rotation stayed.
	assert.equal(fs.existsSync(path.join(dir, 'engine.log.20260924T000000Z')), false);
	assert.ok(archives.some((file) => fs.readFileSync(file, 'utf8') === 'live'));
});

test('skips rotation when free disk is below the minimum', () => {
	const dir = scratchDir('low-disk');
	const log = path.join(dir, 'engine.log');
	fs.writeFileSync(log, 'live');
	setDiskFreeOverride(0);
	rotateLogIfNeeded(log, 1);
	assert.equal(fs.readFileSync(log, 'utf8'), 'live');
	assert.equal(archivePaths(log).length, 0);
});

test('removes a numbered legacy archive once it is old', () => {
	const dir = scratchDir('legacy');
	const log = path.join(dir, 'engine.log');
	const legacy = path.join(dir, 'engine.log.5');
	fs.writeFileSync(legacy, 'legacy');
	setMtime(legacy, 8 * DAY_S);
	pruneArchives(log);
	assert.equal(fs.existsSync(legacy), false);
});

test('appendRotated rotates before a write would cross the cap, never after', () => {
	const dir = scratchDir('append');
	const log = path.join(dir, 'engine.log');
	fs.writeFileSync(log, Buffer.alloc(ENGINE_LOG_MAX_BYTES - 2, 0x61));
	setDiskFreeOverride(HEALTHY_FREE_BYTES);
	appendRotated(log, 'xy'); // exactly at the cap: stays
	assert.equal(archivePaths(log).length, 0);
	appendRotated(log, 'z'); // would cross: rotates first
	assert.equal(archivePaths(log).length, 1);
	assert.equal(fs.readFileSync(log, 'utf8'), 'z');
});

test('the archive stamp is UTC from the ISO form, not local time labeled Z', () => {
	assert.equal(archiveTimestamp(new Date('2026-09-24T01:12:36.906Z')), '20260924T011236Z');
});
