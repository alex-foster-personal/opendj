// requirement STEM-48: the stems "missing" states render what they mean, in
// the browser Stems column (StemTags) and on the deck's stem row (StemRow,
// fed by stem-status.ts). Issue #6241 (GB-15).
// requirement PERFORMANCE-SCREEN-12732: progress has one explicit polite status
// region; ready chips and mixer rows do not duplicate announcements. Issue #12426.
//
// [if] the listing says a bundle is ready [then] StemTags shows V/I/D tags,
//   the meta line, and a title naming model, format and sizes
// [if] the listing says there is no bundle [then] StemTags shows "-" with the
//   title "no local stem bundle"
// [if] the manifest cannot be read [then] StemTags shows "-" and the title
//   names the bundle as invalid with the reason
// [if] the manifest is there but every stem file is gone from disk [then]
//   StemTags shows "-" and the title names the bundle invalid with the missing files
// [if] the deck track has no stems anywhere [then] the deck says NO STEMS and
//   the hover title says none on this machine and none in the cloud
// [if] stems are switched off, or no track is loaded [then] the deck does NOT
//   say NO STEMS
// [if] a stem load is running [then] the deck names the phase, busy
// [if] a stem load failed [then] the deck offers STEMS FAILED - RETRY
//
// Rendered with svelte's own SSR renderer (load-svelte-ssr.mjs): StemTags from
// captured summarizer output; StemRow through the real Deck.svelte and
// ChannelStrip.svelte mounts (same wiring as performance), with deck stem state
// from stem-graph.ts factories seeded into deckStates before render.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const FAKE_NAVIGATION = fileURLToPath(new URL('./fake-app-navigation.mjs', import.meta.url));

const ENTRY = [
	"export { default as StemTags } from '$lib/components/rb/browser/StemTags.svelte';",
	"export { default as Deck } from '$lib/components/rb/Deck.svelte';",
	"export { default as ChannelStrip } from '$lib/components/rb/mixer/ChannelStrip.svelte';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { loadingStemDeckState, readyStemDeckState, unavailableStemDeckState } from '$lib/rb/stem-graph';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	globalThis.requestAnimationFrame = () => 0;
	globalThis.cancelAnimationFrame = () => {};
	mod = await loadSvelteSsrModule(ENTRY, { alias: { '$app/navigation': FAKE_NAVIGATION } });
});

function decode(text) {
	return text
		.replaceAll('&quot;', '"')
		.replaceAll('&#39;', "'")
		.replaceAll('&lt;', '<')
		.replaceAll('&gt;', '>')
		.replaceAll('&amp;', '&');
}

function visibleText(html) {
	return decode(html.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, ''))
		.replace(/\s+/g, ' ')
		.trim();
}

function attr(tag, name) {
	const match = tag.match(new RegExp(`\\s${name}="([^"]*)"`));
	return match === null ? null : decode(match[1]);
}

/** The StemTags trigger span: its opening tag, its inner html, its title. */
function stemTags(stems) {
	const html = mod.render(mod.StemTags, { props: { stems } }).body;
	const match = html.match(/(<span class="stem-tags[^"]*"[^>]*>)([\s\S]*?)<\/span><!---->/);
	assert.ok(match, `StemTags rendered no .stem-tags trigger:\n${html}`);
	const [, open, inner] = match;
	return {
		open,
		empty: /class="stem-tags empty/.test(open),
		title: attr(open, 'title') ?? attr(open, 'aria-label'),
		ariaLabel: attr(open, 'aria-label'),
		text: visibleText(inner),
		onTags: [...inner.matchAll(/<span class="tag[^"]*\bon\b[^"]*"[^>]*data-g="([VID])"/g)].map(
			(m) => m[1]
		)
	};
}

// StemTags: apps/stems/artifacts.py summaries captured in
// ../fixtures/stem-summaries-captured.json (bundles in
// (private path)/). (private path)
// re-runs the summarizer and fails if the summary file drifts.
const SUMMARIES = JSON.parse(
	readFileSync(new URL('../fixtures/stem-summaries-captured.json', import.meta.url), 'utf8')
);

describe('StemTags (browser Stems column)', () => {
	test('stems present: V/I/D tags lit, meta line, title names model, format and sizes', () => {
		const out = stemTags(SUMMARIES.ready);
		assert.equal(out.empty, false);
		assert.deepEqual(out.onTags, ['V', 'I', 'D']);
		assert.equal(out.text, 'VID ht flac 310K');
		assert.equal(
			out.title,
			'htdemucs · flac · total 310K · V=52K (vocals), I=179K (bass+other), D=78K (drums)'
		);
		assert.equal(out.ariaLabel, out.title);
	});

	test('no stems: a dash, and the title says there is no local stem bundle', () => {
		const out = stemTags(SUMMARIES.none);
		assert.equal(out.empty, true);
		assert.equal(out.text, '-');
		assert.equal(out.title, 'no local stem bundle');
		assert.equal(out.ariaLabel, 'no local stem bundle');
	});

	test('summary not loaded for the row: a dash, and the title says so', () => {
		const out = stemTags(null);
		assert.equal(out.empty, true);
		assert.equal(out.text, '-');
		assert.equal(out.title, 'stems not loaded for this row');
	});

	test('bundle recorded but its manifest unreadable: a dash, and the title names it invalid', () => {
		const out = stemTags(SUMMARIES.invalid);
		assert.equal(out.empty, true);
		assert.equal(out.text, '-');
		assert.equal(
			out.title,
			'stem bundle invalid: JSONDecodeError: Expecting value: line 1 column 1 (char 0)'
		);
	});

	test('manifest unreadable with no reason of its own: a dash, and the title falls back to unknown', () => {
		// StemTags.svelte _title: `${s.error ?? 'unknown'}` -- a summary with no
		// error string must still name the state, not render "undefined".
		const out = stemTags({ status: 'invalid' });
		assert.equal(out.empty, true);
		assert.equal(out.text, '-');
		assert.equal(out.title, 'stem bundle invalid: unknown');
	});

	test('bundle recorded but every stem file missing on disk: a dash, and the title names it invalid', () => {
		const out = stemTags(SUMMARIES.all_files_missing);
		assert.equal(out.empty, true);
		assert.equal(out.text, '-');
		assert.equal(
			out.title,
			'stem bundle invalid: every stem file listed in manifest.json is missing: bass.flac, drums.flac, other.flac, vocals.flac'
		);
	});

	test('one stem file missing on disk: that tag stays unlit, the others light', () => {
		const out = stemTags(SUMMARIES.drums_missing);
		assert.deepEqual(out.onTags, ['V', 'I']);
		assert.match(out.title, /D=0B \(\)$/);
	});
});

const noop = async () => {};
const ALIGNMENT = { sample_rate_hz: 44100, frame_count: 100, channel_count: 2, duration_ms: 2 };
const DECK_ID = 1;

function seedDeckStems(stems) {
	mod.deckStates[DECK_ID].stems = stems;
}

/** Render Deck.svelte and read the mounted StemRow (deck scope). */
function deckStemRow(stems) {
	seedDeckStems(stems);
	const html = mod.render(mod.Deck, { props: { deckId: DECK_ID } }).body;
	return parseStemRow(html, 'deck');
}

/** Render ChannelStrip.svelte and read its StemRow (channel scope, no onLoad). */
function channelStemRow(stems) {
	seedDeckStems(stems);
	const props = {
		deckId: DECK_ID,
		less: false,
		trim: 0.5,
		eqHigh: 0.5,
		eqMid: 0.5,
		eqLow: 0.5,
		filter: 0.5,
		fader: 1,
		cueEnabled: false,
		stemEqMode: false,
		stemPending: false,
		commandPending: false,
		commandError: null,
		ontrim: noop,
		oneq: noop,
		onfilter: noop,
		onfader: noop,
		oncue: noop,
		onStemEqMode: noop,
		onStemGain: noop,
		onStemMute: noop,
		onStemSolo: noop
	};
	const html = mod.render(mod.ChannelStrip, { props }).body;
	return parseStemRow(html, 'channel');
}

function parseStemRow(html, scope) {
	const group = html.match(/<div class="stems[^"]*"[^>]*>/);
	assert.ok(group, `StemRow rendered no .stems group:\n${html}`);
	const status = html.match(
		new RegExp(
			`(<(span|button) class="status[^"]*"[^>]*data-testid="stem-status-${scope}-${DECK_ID}"[^>]*>)([\\s\\S]*?)<\\/\\2>`
		)
	);
	return {
		groupRole: attr(group[0], 'role'),
		groupLive: attr(group[0], 'aria-live'),
		groupTitle: attr(group[0], 'title'),
		groupState: attr(group[0], 'data-stems-state'),
		groupPhase: attr(group[0], 'data-stems-phase'),
		statusCount: [
			...html.matchAll(new RegExp(`data-testid="stem-status-${scope}-${DECK_ID}"`, 'g'))
		].length,
		labelRole: status === null ? null : attr(status[1], 'role'),
		labelLive: status === null ? null : attr(status[1], 'aria-live'),
		label: status === null ? null : visibleText(status[3]),
		labelTitle: status === null ? null : attr(status[1], 'title'),
		labelTag: status === null ? null : status[2],
		labelAction: status === null ? null : attr(status[1], 'data-stems-action'),
		labelBusy: status === null ? null : /class="status[^"]*\bbusy\b/.test(status[1]),
		chips: [...html.matchAll(/<button class="chip[^"]*"[^>]*>/g)].map((m) => ({
			role: attr(m[0], 'role'),
			live: attr(m[0], 'aria-live'),
			label: attr(m[0], 'aria-label'),
			pressed: attr(m[0], 'aria-pressed')
		})),
		chipsDisabled: [...html.matchAll(/<button class="chip[^"]*"[^>]*>/g)].map((m) =>
			/\sdisabled=""/.test(m[0])
		)
	};
}

describe('StemRow NO STEMS label (deck via Deck.svelte)', () => {
	test('no stems anywhere: NO STEMS, hover says none on this machine and none in the cloud', () => {
		const out = deckStemRow(mod.unavailableStemDeckState('no stem bundle for t-42'));
		const tip =
			'This track has no stems: none on this machine and none in the cloud. (no stem bundle for t-42)';
		assert.equal(out.groupState, 'none');
		assert.equal(out.groupPhase, null);
		assert.equal(out.label, 'NO STEMS');
		assert.equal(out.labelTag, 'span');
		assert.equal(out.labelBusy, false);
		assert.equal(out.labelTitle, tip);
		assert.equal(out.groupTitle, tip);
		assert.deepEqual(out.chipsDisabled, [true, true, true]);
	});

	test('stems switched off by a mode: STEMS OFF, never NO STEMS', () => {
		const out = deckStemRow(mod.unavailableStemDeckState('stems disabled: less mode'));
		assert.equal(out.groupState, 'off');
		assert.equal(out.groupPhase, null);
		assert.equal(out.label, 'STEMS OFF');
		assert.equal(out.labelTitle, 'Stems are switched off: less mode.');
	});

	test('empty deck: no label at all, and the hover does not claim the track has no stems', () => {
		const out = deckStemRow(mod.unavailableStemDeckState(null));
		assert.equal(out.groupState, 'empty');
		assert.equal(out.groupPhase, null);
		assert.equal(out.label, null);
		assert.equal(
			out.groupTitle,
			'Stem buttons switch on when a track with stems is loaded on this deck.'
		);
	});

	test('stems present: no label, chips live, hover explains the chips', () => {
		const out = deckStemRow(
			mod.readyStemDeckState({ source: 'demucs', model: 'htdemucs', layout: 'demucs4' }, ALIGNMENT)
		);
		assert.equal(out.statusCount, 0);
		assert.equal(out.groupRole, 'group');
		assert.equal(out.groupLive, null);
		assert.deepEqual(
			out.chips,
			['VOCAL', 'INST', 'DRUMS', 'BASS', 'HARM'].map((label) => ({
				role: null,
				live: null,
				label: `${label} stem mute deck ${DECK_ID}; Shift+click solo`,
				pressed: 'false'
			}))
		);
		assert.equal(out.groupState, 'ready');
		assert.equal(out.groupPhase, null);
		assert.equal(out.label, null);
		assert.equal(out.groupTitle, 'real htdemucs stems (demucs4) - click mute, Shift+click solo');
		assert.deepEqual(out.chipsDisabled, [false, false, false, false, false]);
	});

	test('stem load without published load detail: probing state, phase attribute omitted', () => {
		const stems = { ...mod.loadingStemDeckState(), load: null };
		const out = deckStemRow(stems);
		assert.equal(out.groupState, 'probing');
		assert.equal(out.groupPhase, null);
	});

	test('stem load running (checking): CHECKING STEMS, busy, hover says the deck already plays', () => {
		const out = deckStemRow(mod.loadingStemDeckState());
		assert.equal(out.groupState, 'probing');
		assert.equal(out.groupPhase, 'probing');
		assert.equal(out.label, 'CHECKING STEMS');
		assert.equal(out.labelBusy, true);
		assert.equal(
			out.labelTitle,
			'Checking whether this track has stems. The deck already plays; the stem buttons switch on when this finishes.'
		);
	});

	test('stem load running (cloud fetch): FETCHING STEMS with file progress, busy', () => {
		const out = deckStemRow(
			mod.loadingStemDeckState({
				phase: 'fetching',
				progress: { files_total: 5, files_done: 2, bytes_done: 12_900_000 },
				reason: null
			})
		);
		assert.equal(out.groupPhase, 'fetching');
		assert.equal(out.label, 'FETCHING STEMS 2/5');
		assert.equal(out.statusCount, 1);
		assert.equal(out.labelTag, 'span');
		assert.equal(out.labelRole, 'status');
		assert.equal(out.labelLive, 'polite');
		const progressed = deckStemRow(
			mod.loadingStemDeckState({
				phase: 'fetching',
				progress: { files_total: 5, files_done: 3, bytes_done: 18_000_000 },
				reason: null
			})
		);
		assert.equal(progressed.label, 'FETCHING STEMS 3/5');
		assert.equal(progressed.statusCount, 1);
		assert.equal(progressed.labelRole, 'status');
		assert.equal(progressed.labelLive, 'polite');
		assert.equal(out.labelBusy, true);
		assert.match(
			out.labelTitle,
			/^Fetching this track's stems from the cloud: 2 of 5 files, 12\.3 MB so far\./
		);
	});

	test('stem load running (fetch, progress not reported yet): FETCHING STEMS, no count', () => {
		const out = deckStemRow(
			mod.loadingStemDeckState({ phase: 'fetching', progress: null, reason: null })
		);
		assert.equal(out.groupPhase, 'fetching');
		assert.equal(out.label, 'FETCHING STEMS');
		assert.equal(out.labelBusy, true);
		assert.match(
			out.labelTitle,
			/^Fetching this track's stems from the cloud\. The deck already plays/
		);
	});

	test('stem load running (local read, decode, switch): each phase has its own label', () => {
		const phases = [
			['downloading', 'LOADING STEMS', /^Reading the stem files from this machine\./],
			['decoding', 'DECODING STEMS', /^Decoding the stem audio\./],
			[
				'switching',
				'SWITCHING TO STEMS',
				/^The stems are decoded and are taking over the playing deck/
			]
		];
		for (const [phase, label, tip] of phases) {
			const out = deckStemRow(mod.loadingStemDeckState({ phase, progress: null, reason: null }));
			assert.equal(out.groupState, phase);
			assert.equal(out.groupPhase, phase, phase);
			assert.equal(out.label, label, phase);
			assert.equal(out.labelTag, 'span', phase);
			assert.equal(out.statusCount, 1, phase);
			assert.equal(out.labelRole, 'status', phase);
			assert.equal(out.labelLive, 'polite', phase);
			assert.equal(out.labelBusy, true, phase);
			assert.match(out.labelTitle, tip, phase);
		}
	});

	test('stem load held: STEMS WAITING - LOAD NOW is a load_now button with the reason', () => {
		const out = deckStemRow(
			mod.loadingStemDeckState({
				phase: 'waiting',
				progress: null,
				reason: 'memory pressure'
			})
		);
		assert.equal(out.groupPhase, 'waiting');
		assert.equal(out.label, 'STEMS WAITING - LOAD NOW');
		assert.equal(out.labelTag, 'button');
		assert.equal(out.labelAction, 'load_now');
		assert.equal(out.labelRole, null);
		assert.equal(out.labelLive, null);
		assert.equal(out.labelTitle, 'Stems are held back: memory pressure. Click to load them now.');
	});

	test('stem load failed: STEMS FAILED - RETRY is a retry button with the error', () => {
		const failed = { ...mod.unavailableStemDeckState('decode failed: bad frame'), status: 'error' };
		const out = deckStemRow(failed);
		assert.equal(out.groupState, 'error');
		assert.equal(out.groupPhase, null);
		assert.equal(out.label, 'STEMS FAILED - RETRY');
		assert.equal(out.labelTag, 'button');
		assert.equal(out.labelAction, 'retry');
		assert.equal(out.labelRole, null);
		assert.equal(out.labelLive, null);
		assert.equal(out.labelTitle, 'Stems failed to load: decode failed: bad frame. Click to retry.');
	});
});

describe('StemRow mixer strip (via ChannelStrip.svelte)', () => {
	test('fetching and decoding do not duplicate the deck progress status', () => {
		for (const phase of ['fetching', 'decoding']) {
			const out = channelStemRow(
				mod.loadingStemDeckState({
					phase,
					progress: { files_total: 5, files_done: 2, bytes_done: 12_900_000 },
					reason: null
				})
			);
			assert.equal(out.label, null, phase);
			assert.equal(out.statusCount, 0, phase);
			assert.equal(out.groupRole, 'group', phase);
			assert.equal(out.groupLive, null, phase);
		}
	});

	test('mixer strip: no NO STEMS label, the reason stays on hover', () => {
		const out = channelStemRow(mod.unavailableStemDeckState('no stem bundle for t-42'));
		assert.equal(out.label, null);
		assert.equal(
			out.groupTitle,
			'This track has no stems: none on this machine and none in the cloud. (no stem bundle for t-42)'
		);
	});
});
