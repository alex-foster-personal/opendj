/**
 * Key and BPM auto-fit measures the text in the active skin's font (the maintainer,
 * Tue 27 Oct 2026: Gothic ellipsized the Key and BPM columns, because the old
 * fit guessed 7px per character and capped Key at 36px while Gothic renders
 * JetBrains Mono at 10.5px and the default notation became `8A • Am`).
 *
 * One pure test per skin. The measurer is built from the font each skin
 * declares in theme.css (parsed below, the same values the browser fit reads
 * back from computed style at runtime):
 * - Gothic and Light: JetBrains Mono, falling back to SF Mono / Menlo. All
 *   three are monospaced with a 0.6em advance at every weight (JetBrains Mono
 *   600/1000, SF Mono 1229/2048, Menlo 1233/2048), so chars x 0.6 x size is
 *   the real rendered width.
 * - Default: a proportional system stack (SF Pro / Inter). The browser fit
 *   measures it with canvas; this test uses a deliberately WIDE stand-in
 *   (0.62em lowercase and digits, 0.78em capitals, 0.5em bullet and space)
 *   so the contract "column >= text + padding" is checked against an upper
 *   bound, not a flattering guess.
 *
 * [if] a skin's column is narrower than any key, status word or header it prints [then] the cell clips text - broken.
 * [if] the fit stops reading the rendered font [then] it measures the wrong skin - broken.
 * [if] Key or BPM cells ellipsize [then] a two-character value loses a character to "…" - broken.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (rel) => readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');

let fit;
let notation;
let skins;

before(async () => {
	fit = await loadTypeScriptModule('src/lib/rb/musical-column-fit.ts');
	notation = await loadTypeScriptModule('src/lib/rb/key-notation.ts');
	skins = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
});

function parseFont(font) {
	const m = /^(\d+) ([\d.]+)px (.+)$/.exec(font);
	assert.ok(m, `unparseable font string ${font}`);
	return { weight: Number(m[1]), size: Number(m[2]), family: m[3] };
}

function monoMeasure(text, font) {
	const { size, family } = parseFont(font);
	assert.match(family, /JetBrains Mono/, 'mono measurer handed a non-mono font');
	return [...text].length * 0.6 * size;
}

function wideProportionalMeasure(text, font) {
	const { size, weight } = parseFont(font);
	let em = 0;
	for (const ch of text) {
		if (ch === ' ' || ch === '•') em += 0.5;
		else if (/[A-Z#]/.test(ch)) em += 0.78;
		else em += 0.62;
	}
	return em * size * (weight === 700 ? 1.06 : 1);
}

const NOTATIONS = ['camelot_musical', 'camelot', 'open_key', 'musical'];

const MEASURERS = { default: wideProportionalMeasure, 'mono-dev': monoMeasure, light: monoMeasure, 'green-room': wideProportionalMeasure, 'practice-ddj-flx4': wideProportionalMeasure };

/** theme.css block for a skin: default is :root, others html[data-skin=...] */
function themeFont(skin) {
	const css = read('src/lib/rb/theme.css');
	const fonts = [...css.matchAll(/--rb-font:\s*([^;]+);\s*\n?\s*--rb-fs-browser:\s*([\d.]+)px/g)].map((m) => ({
		family: m[1].replace(/\s+/g, ' ').trim(),
		size: Number(m[2]),
		index: m.index
	}));
	const blockStart = skin === 'default' ? 0 : css.indexOf(`html[data-skin='${skin}'] {`);
	assert.ok(blockStart >= 0, `theme.css has no block for skin ${skin}`);
	const candidates = fonts.filter((f) => f.index >= blockStart);
	assert.ok(candidates.length > 0, `theme.css declares no --rb-font for ${skin}`);
	return candidates[0];
}

function rowsForEveryKey() {
	const rows = [];
	for (let n = 1; n <= 12; n += 1) {
		for (const mode of ['A', 'B']) rows.push({ key: `${n}${mode}`, bpm: 174.5, bpm_status: 'ok' });
	}
	rows.push({ key: null, key_status: 'missing', bpm: null, bpm_status: 'missing' });
	rows.push({ key: null, key_status: 'failed', bpm: null, bpm_status: 'failed' });
	return rows;
}

describe('Key and BPM columns fit their text in every skin', () => {
	it('covers every skin the app ships', () => {
		assert.deepEqual(Object.keys(MEASURERS).sort(), [...skins.UI_SKIN_CHOICES].sort());
		assert.deepEqual([...notation.KEY_NOTATIONS], NOTATIONS);
	});

	for (const skin of ['default', 'mono-dev', 'light', 'green-room', 'practice-ddj-flx4']) {
		it(`${skin}: theme.css declares its table font`, () => {
			const declared = themeFont(skin);
			assert.ok(declared.size > 0 && declared.family.length > 0);
			if (skin === 'mono-dev' || skin === 'light') assert.match(declared.family, /^'JetBrains Mono'/, 'the mono measurer no longer matches this skin');
		});

		for (const padX of [6, 8]) {
			for (const notationId of NOTATIONS) {
				it(`${skin}, pad ${padX}, ${notationId}: every key, status word and header fits`, () => {
					const measure = MEASURERS[skin];
					const declared = themeFont(skin);
					const font = { family: declared.family, sizePx: declared.size };
					const rows = rowsForEveryKey();
					const keyHeader = notation.formatKey('12A', notationId);
					const out = fit.fitMusicalColumns({
						rows,
						notation: notationId,
						font,
						padX,
						keyHeader,
						bpmHeader: '174.5',
						measure
					});
					const body = fit.cssFont(font);
					const bold = fit.cssFont(font, 700);
					for (const text of [...notation.allKeyTexts(notationId), 'missing', 'failed']) {
						assert.ok(
							out.key >= measure(text, body) + 2 * padX,
							`Key ${out.key}px cannot show ${text} (${measure(text, body)}px)`
						);
					}
					for (const text of ['174.5', '--', 'missing', 'failed']) {
						assert.ok(out.bpm >= measure(text, body) + 2 * padX, `BPM ${out.bpm}px cannot show ${text}`);
					}
					assert.ok(out.key >= measure(keyHeader, bold) + 11 + 2 * padX, 'Key header cut off');
					assert.ok(out.bpm >= measure('174.5', bold) + 11 + 2 * padX, 'BPM header cut off');
				});
			}
		}
	}

	it('Gothic: the default notation needs far more than the old 36px cap', () => {
		const out = fit.fitMusicalColumns({
			rows: [{ key: '12A', bpm: 128, bpm_status: 'ok' }],
			notation: 'camelot_musical',
			font: { family: themeFont('mono-dev').family, sizePx: themeFont('mono-dev').size },
			padX: 6,
			keyHeader: 'K',
			bpmHeader: 'B',
			measure: monoMeasure
		});
		// "12A • Dbm" is 9 monospaced cells: 9 x 6.3 = 56.7px + 12px padding.
		assert.ok(out.key >= 69, `got ${out.key}`);
	});

	it('a long unparsed stored key is measured in full, never capped', () => {
		const raw = 'Unknown key from a tagger 1234567890';
		const font = { family: themeFont('mono-dev').family, sizePx: themeFont('mono-dev').size };
		const out = fit.fitMusicalColumns({ rows: [{ key: raw, bpm: 128, bpm_status: 'ok' }], notation: 'camelot', font, padX: 6, keyHeader: 'Key', bpmHeader: 'BPM', measure: monoMeasure });
		assert.ok(out.key >= monoMeasure(raw, fit.cssFont(font)) + 12, `capped at ${out.key}`);
	});

	it('a measurer that returns garbage fails loudly instead of sizing to it', () => {
		assert.throws(
			() =>
				fit.fitMusicalColumns({
					rows: [],
					notation: 'camelot',
					font: { family: themeFont('default').family, sizePx: themeFont('default').size },
					padX: 6,
					keyHeader: 'K',
					bpmHeader: 'B',
					measure: () => Number.NaN
				}),
			/measureText returned NaN/
		);
	});

	it('Key and BPM cells clip instead of ellipsizing, and the table uses the measured fit', () => {
		const table = read('src/lib/components/rb/browser/TrackTable.svelte');
		const style = table.slice(table.lastIndexOf('<style>')).replace(/\/\*[\s\S]*?\*\//g, '');
		assert.match(style, /\.c-key,\s*\.c-bpm\s*\{\s*text-overflow:\s*clip;\s*\}/);
		assert.match(table, /fitMusicalColumns\(\{/);
		assert.match(table, /measure: canvasMeasureText/);
		assert.match(table, /font: renderedTableFont\(wrapEl\)/);
		assert.doesNotMatch(table, /compactMusicalWidths/);
	});

	it('the Key and BPM headers name the column and never print the master deck\'s value (the maintainer, Tue 27 Oct 2026)', () => {
		const table = read('src/lib/components/rb/browser/TrackTable.svelte');
		const header = (cls) => {
			const start = table.indexOf(`class="h-${cls} sortable"`);
			assert.ok(start > 0, `no ${cls} header`);
			return table.slice(start, table.indexOf('</th>', start));
		};
		for (const [cls, word] of [['key', 'Key'], ['bpm', 'BPM']]) {
			const th = header(cls);
			assert.match(th, new RegExp(`<span>${word}</span>`), `${cls} header does not read ${word}`);
			assert.doesNotMatch(th, /masterKey|masterBpm|_fmtBpm|th-master-val/, `${cls} header prints the master's value`);
			assert.match(th, new RegExp(`'${word} - sort by ${cls === 'key' ? 'key' : 'BPM'} \\(asc`));
		}
		assert.match(table, /keyHeader: 'Key',\s*bpmHeader: 'BPM',/, 'the fit measures a header the table no longer prints');
	});

	for (const skin of ['default', 'mono-dev', 'light', 'green-room', 'practice-ddj-flx4']) {
		for (const padX of [6, 8]) {
			it(`${skin}, pad ${padX}: "Key" and "BPM" headers fit with the sort arrow, unclipped`, () => {
				const declared = themeFont(skin);
				const font = { family: declared.family, sizePx: declared.size };
				const measure = MEASURERS[skin];
				const out = fit.fitMusicalColumns({ rows: [], notation: 'camelot', font, padX, keyHeader: 'Key', bpmHeader: 'BPM', measure });
				const bold = fit.cssFont(font, 700);
				assert.ok(out.key >= measure('Key', bold) + 11 + 2 * padX, `Key header cut at ${out.key}px`);
				assert.ok(out.bpm >= measure('BPM', bold) + 11 + 2 * padX, `BPM header cut at ${out.bpm}px`);
			});
		}
	}
});
