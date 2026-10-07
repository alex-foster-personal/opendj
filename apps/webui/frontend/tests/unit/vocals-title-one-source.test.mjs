/**
 * VOCALS-02 (orders board #5638 W36; the maintainer: "shouldn't say not analyzed in rbx
 * just 'vocals not found/analyzed'"): the vocal lane tooltip comes from ONE
 * module, and no wording names rekordbox.
 *
 * Regression lines:
 * - if any vocals status maps to a string naming rekordbox, then the maintainer's ask is broken
 * - if a status with no regions shows a source label or nothing, then absence is ambiguous
 * - if StripWaveform or PreviewStrip carries its own copy of the wording again, then it can drift
 * - if any tooltip literal appears in a second module under src/, then there are two sources
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const REGION = { start_s: 1, end_s: 2, intensity: 1 };

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/wave/vocals-title.ts');
});

test('every vocals status has the agreed wording and none names rekordbox', () => {
	const cases = [
		[null, null],
		[{ status: 'not_analyzed' }, 'vocals not analyzed yet'],
		[{ status: 'no_vocals', fps: 150, regions: [] }, 'no vocals found'],
		[{ status: 'demucs', fps: 150, regions: [REGION] }, 'vocals: local detection'],
		[{ status: 'demucs', fps: 150, regions: [] }, 'no vocals found'],
		[{ status: 'rekordbox', fps: 150, regions: [REGION] }, null],
		[{ status: 'rekordbox', fps: 150, regions: [] }, 'no vocals found']
	];
	for (const [vocals, want] of cases) {
		const got = mod.vocalsStatusTitle(vocals);
		assert.equal(got, want, JSON.stringify(vocals));
		if (got !== null) assert.doesNotMatch(got, /rekordbox|rbx/i);
	}
	assert.throws(() => mod.vocalsStatusTitle({ status: 'bogus' }), /unknown status/);
});

function walk(dir, out = []) {
	for (const name of readdirSync(dir)) {
		const p = join(dir, name);
		if (statSync(p).isDirectory()) walk(p, out);
		else if (/\.(ts|svelte)$/.test(name)) out.push(p);
	}
	return out;
}

test('each tooltip literal lives in exactly one module, vocals-title.ts', () => {
	const files = walk(SRC);
	assert.ok(files.length > 100, `walked ${files.length} files; the walk is not finding src`);
	const literals = [
		mod.VOCALS_NOT_ANALYZED_TITLE,
		mod.VOCALS_NONE_FOUND_TITLE,
		mod.VOCALS_LOCAL_DETECTION_TITLE,
		'vocals not analyzed in rekordbox',
		'no vocals detected'
	];
	for (const literal of literals) {
		const hits = files
			.filter((f) => readFileSync(f, 'utf8').includes(`'${literal}'`))
			.map((f) => f.slice(SRC.length + 1));
		const allowed = literal.includes('rekordbox') || literal === 'no vocals detected' ? [] : ['lib/components/rb/wave/vocals-title.ts'];
		assert.deepEqual(hits, allowed, `'${literal}'`);
	}
});

test('the deck strip and the preview strip import the one source', () => {
	for (const rel of ['lib/components/rb/deck/StripWaveform.svelte', 'lib/components/rb/browser/PreviewStrip.svelte']) {
		const src = readFileSync(join(SRC, rel), 'utf8');
		assert.match(src, /import \{ vocalsStatusTitle \} from '\$lib\/components\/rb\/wave\/vocals-title'/, rel);
		assert.match(src, /\$derived\(vocalsStatusTitle\(vocals\)\)/, rel);
	}
});
