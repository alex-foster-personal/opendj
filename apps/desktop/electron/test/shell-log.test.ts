import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { EngineError, appendShellLogToPath, installShellLogPath, logTail, utcTimestampIso } from '../src/shell-log';
import { scratchDir } from './helpers';

test('shell lines are appended with the [shell LEVEL] prefix', () => {
	const log = path.join(scratchDir('shell-log'), 'engine.log');
	appendShellLogToPath(log, 'WARN', 'monitor scale factor 0');
	assert.match(fs.readFileSync(log, 'utf8'), /\[shell WARN\] monitor scale factor 0\n/);
});

test('a vanished log directory does not throw', () => {
	const dir = scratchDir('shell-log-missing');
	const log = path.join(dir, 'engine.log');
	appendShellLogToPath(log, 'WARN', 'before');
	fs.rmSync(dir, { recursive: true });
	appendShellLogToPath(log, 'panic', 'probe');
});

test('a second install is refused, naming both paths, and the first is kept', () => {
	const first = path.join(scratchDir('first'), 'engine.log');
	const second = path.join(scratchDir('second'), 'engine.log');
	const slot = { path: null as string | null };
	installShellLogPath(slot, first);
	assert.throws(
		() => installShellLogPath(slot, second),
		(error: unknown) =>
			error instanceof EngineError &&
			/already installed/.test(error.headline) &&
			error.detail.includes(first) &&
			error.detail.includes(second)
	);
	assert.equal(slot.path, first);
});

test('logTail returns the last lines, and nothing for a missing file', () => {
	const log = path.join(scratchDir('tail'), 'engine.log');
	assert.equal(logTail(log, 2), '');
	fs.writeFileSync(log, 'a\nb\nc\n');
	assert.equal(logTail(log, 2), 'b\nc');
});

test('UTC timestamps end +00:00, as the Rust shell wrote them', () => {
	assert.equal(utcTimestampIso(new Date('2026-09-24T01:02:03.004Z')), '2026-09-24T01:02:03.004+00:00');
});
