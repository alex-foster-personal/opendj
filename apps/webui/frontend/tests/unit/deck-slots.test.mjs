import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Regression Mon 17 Aug 2026: "double clicking two songs in succession should
// load two songs into two decks; cmd+doubleclick should replace the song
// already loaded if you've changed your mind." The old inline picker only
// bumped deckLoadSeq AFTER the async load resolved, so two fast double-
// clicks (before the first load settled) both read the same stale seq and
// picked the SAME deck - the second track silently replaced the first.
// - if the picker itself needs a caller-side async round trip to update
//   ordering then two fast double-clicks race and collide -- broken (this
//   is exactly why the picker is now a pure, synchronous function: callers
//   reserve the returned deck immediately, not after the load resolves)
// - if replace=true with no prior double-click falls to null instead of the
//   normal pick then a bare cmd+dblclick before ever double-clicking does
//   nothing -- broken
// - if replace=true ignores lastDoubleClickDeck and advances anyway then
//   "changed my mind" adds a third deck instead of swapping -- broken
// - if shift picks a deck that's currently playing then it silently steals
//   audio out from under the DJ -- broken

async function _mod() {
	return loadTypeScriptModule('src/lib/rb/deck-slots.ts');
}

/** Four idle decks; `overrides` sets per-deck fields. The picker took a
 * deckLoadSeq map and a CH3/CH4 pair before pin d2c156a503bb; it now takes the
 * full slot state, because master and playing had to become visible to it. */
function decksOf(overrides = {}) {
	const base = {};
	for (const d of [1, 2, 3, 4]) {
		base[d] = {
			stable_id: null,
			playing: false,
			is_master: false,
			fader: 0.8,
			loadSeq: 0,
			reservationPending: false
		};
	}
	for (const [k, v] of Object.entries(overrides)) base[k] = { ...base[k], ...v };
	return base;
}

describe('double-click deck pick', () => {
	it('two plain double-clicks in a row pick two different decks (no async round trip needed)', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const first = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: decksOf(),
			lastDoubleClickDeck: null
		});
		assert.equal(first.deck, 1);
		// The caller reserves deck 1 synchronously, before any await - it now
		// fills the slot rather than only bumping a counter. Simulated here
		// exactly as BrowserPanel does it.
		const second = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: decksOf({ 1: { stable_id: 'a', loadSeq: 1 } }),
			lastDoubleClickDeck: 1
		});
		assert.equal(second.deck, 2);
	});

	it('cmd/ctrl+dblclick replaces the last double-click deck instead of advancing', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const result = pickDoubleClickDeck({
			shift: false,
			replace: true,
			decks: decksOf({ 1: { stable_id: 'a', loadSeq: 1 } }),
			lastDoubleClickDeck: 1
		});
		assert.equal(result.deck, 1);
	});

	it('cmd/ctrl+dblclick before any plain double-click falls through to the normal pick', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const result = pickDoubleClickDeck({
			shift: false,
			replace: true,
			decks: decksOf(),
			lastDoubleClickDeck: null
		});
		assert.equal(result.deck, 1);
	});

	it('shift prefers an empty CH3/CH4 deck over a stopped one, and never a playing one', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const bothPlaying = pickDoubleClickDeck({
			shift: true,
			replace: false,
			decks: decksOf({
				3: { stable_id: 'a', playing: true },
				4: { stable_id: 'b', playing: true }
			}),
			lastDoubleClickDeck: null
		});
		assert.equal(bothPlaying.deck, null);
		assert.match(bothPlaying.error, /both playing/);

		const oneEmpty = pickDoubleClickDeck({
			shift: true,
			replace: false,
			decks: decksOf({
				3: { stable_id: 'a', playing: true },
				4: { stable_id: null, playing: false }
			}),
			lastDoubleClickDeck: null
		});
		assert.equal(oneEmpty.deck, 4);
	});
});

// ---------------------------------------------------------------------------
// Pin d2c156a503bb (the maintainer, Wed 2 Sep 2026): "I just double clicked a track
// (bloodstream) and it replaced the ACTIVE MASTER instead of the non-active
// master. Should have preference for what to replace that goes 1,2,3,4 but
// more importantly stopped/finished/empty 1/2 then S/F/E 3/4 then if all busy
// the least active deck (eg volume 0, last update most stale), and I think
// basically never replaces Master?"
//
// The old picker read ONLY deckLoadSeq for CH1/CH2, so master and playing were
// invisible to it: the least-recently-loaded deck wins even while it is the
// one feeding the speakers. That is the worst possible slot to take mid-set,
// and it is the default gesture.
//
// - if the picker can return the master deck while another slot is free then
//   a double-click can cut the live output -- broken
// - if a playing deck is chosen while a stopped one is free then the gesture
//   steals audio it did not have to -- broken
// - if an empty deck is not preferred over a stopped one then a loaded track
//   is discarded for no reason -- broken
// - if every deck is busy and the picker returns null then the gesture stops
//   working entirely instead of degrading to a sensible victim -- broken
describe('pickDoubleClickDeck: never take the master, prefer idle slots', () => {
	function state(over = {}) {
		const base = {
			1: { stable_id: 'a', playing: true, is_master: true, fader: 0.8, loadSeq: 1 },
			2: { stable_id: 'b', playing: true, is_master: false, fader: 0.8, loadSeq: 2 },
			3: { stable_id: 'c', playing: true, is_master: false, fader: 0.8, loadSeq: 3 },
			4: { stable_id: 'd', playing: true, is_master: false, fader: 0.8, loadSeq: 4 }
		};
		for (const [k, v] of Object.entries(over)) base[k] = { ...base[k], ...v };
		return base;
	}

	it('d2c156a503bb double-click never targets the live master deck', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const out = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: state({ 2: { stable_id: null, playing: false } })
		});
		assert.notEqual(out.deck, 1, 'the picker took the master deck');
		assert.equal(out.deck, 2);
	});

	it('never returns the master while any other slot is available', async () => {
		const { pickDoubleClickDeck } = await _mod();
		// Deck 1 is master AND least-recently-loaded: the old rule picked it.
		const out = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: state({ 2: { stable_id: null, playing: false } })
		});
		assert.notEqual(out.deck, 1, 'the picker took the master deck');
		assert.equal(out.deck, 2);
	});

	it('prefers empty, then stopped, over anything playing', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const empty = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: state({ 3: { stable_id: null, playing: false }, 4: { playing: false } })
		});
		assert.equal(empty.deck, 3, 'an empty deck should win over a merely stopped one');

		const stopped = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: state({ 4: { playing: false } })
		});
		assert.equal(stopped.deck, 4, 'a stopped deck should win over playing ones');
	});

	it('prefers CH1/CH2 over CH3/CH4 at equal idleness', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const out = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: state({
				1: { is_master: false },
				2: { stable_id: null, playing: false },
				3: { stable_id: null, playing: false }
			})
		});
		assert.equal(out.deck, 2, 'CH2 and CH3 both empty - the 1,2,3,4 preference picks CH2');
	});

	it('falls back to the least active deck when everything is busy', async () => {
		const { pickDoubleClickDeck } = await _mod();
		// All playing, none empty. Deck 3 is faded out, so it is least active.
		const out = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: state({ 3: { fader: 0 } })
		});
		assert.equal(out.deck, 3);
		assert.equal(out.error, undefined, 'the gesture must still work when all decks are busy');
	});

	it('still refuses to take the master even when it is the least active', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const out = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: state({ 1: { fader: 0 } })
		});
		assert.notEqual(out.deck, 1);
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3917231692
// (P1/BLOCKING): a cached replace target that became master since the plain
// double-click that cached it must not be reused - the early return used to
// bypass the master exclusion entirely.
describe('replace target revalidated against current master state', () => {
	it('falls through to the normal pick when the cached deck has since become master', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const out = pickDoubleClickDeck({
			shift: false,
			replace: true,
			decks: decksOf({
				1: { is_master: true }, // cached target, now master
				2: { stable_id: null } // next-best eligible slot
			}),
			lastDoubleClickDeck: 1
		});
		assert.notEqual(out.deck, 1, 'reused a cached deck that is now the live master');
		assert.equal(out.deck, 2);
	});

	it('still reuses the cached deck when it has NOT become master', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const out = pickDoubleClickDeck({
			shift: false,
			replace: true,
			decks: decksOf({ 1: { stable_id: 'a', loadSeq: 1 } }),
			lastDoubleClickDeck: 1
		});
		assert.equal(out.deck, 1);
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3918252794
// (P1/BLOCKING): BrowserPanel._reserveDeckSlot() bumps loadSeq synchronously,
// before stable_id publishes. Two plain double-clicks arriving before the
// first async load resolves must not both read the reserved deck as "empty".
describe('empty-tier candidates honor an in-flight synchronous reservation', () => {
	it('a second double-click does not collide with a deck reserved but not yet loaded', async () => {
		const { pickDoubleClickDeck } = await _mod();
		// Deck 1 was just reserved by _reserveDeckSlot (loadSeq bumped) but its
		// async load has not published stable_id yet - it still reads empty by
		// stable_id alone. Deck 2 has never been touched (loadSeq 0).
		const second = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: decksOf({ 1: { stable_id: null, loadSeq: 1 } }),
			lastDoubleClickDeck: 1
		});
		assert.equal(second.deck, 2, 'picked the deck already reserved by the first double-click');
	});

	it('among untouched empty decks (equal loadSeq), the 1,2,3,4 preference still wins', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const out = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: decksOf(),
			lastDoubleClickDeck: null
		});
		assert.equal(out.deck, 1);
	});

	// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3919597969
	// (P1/BLOCKING, found on the empty-tier fix above): the stopped and
	// silent tiers filter on `playing`/`fader`, neither of which changes the
	// instant _reserveDeckSlot bumps loadSeq - only stable_id does, later,
	// once the async load/unload settles. So the race above reproduces in
	// EVERY tier, not just empty.
	it('two loaded-but-stopped decks: a second double-click does not collide with the first reservation', async () => {
		const { pickDoubleClickDeck } = await _mod();
		// Both CH1 and CH2 are stopped (loaded, not playing) - the first
		// double-click reserves CH1 for an unload+reload that has not
		// resolved yet, so CH1 still reads stopped, not empty. CH3/CH4 are
		// playing so they cannot shadow the stopped tier under test.
		const second = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: decksOf({
				1: { stable_id: 'a', playing: false, loadSeq: 1 },
				2: { stable_id: 'b', playing: false, loadSeq: 0 },
				3: { stable_id: 'c', playing: true },
				4: { stable_id: 'd', playing: true }
			}),
			lastDoubleClickDeck: null
		});
		assert.equal(second.deck, 2, 'picked the stopped deck already reserved by the first double-click');
	});

	it('two loaded-but-silent decks: a second double-click does not collide with the first reservation', async () => {
		const { pickDoubleClickDeck } = await _mod();
		// CH3/CH4 are playing and audible so they cannot shadow the silent
		// tier under test (only CH1/CH2 are candidates: playing but faded).
		const second = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: decksOf({
				1: { stable_id: 'a', playing: true, fader: 0, loadSeq: 1 },
				2: { stable_id: 'b', playing: true, fader: 0, loadSeq: 0 },
				3: { stable_id: 'c', playing: true },
				4: { stable_id: 'd', playing: true }
			}),
			lastDoubleClickDeck: null
		});
		assert.equal(second.deck, 2, 'picked the silent deck already reserved by the first double-click');
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3919725002
// (P1/BLOCKING, found on the stopped/silent-tier fix above): the two tests
// above still let a pending reservation win when it is the ONLY candidate in
// its own tier - an empty (or stopped, or silent) singleton always wins its
// tier before any within-tier loadSeq comparison runs, so there is nothing
// left in that tier to compare against and fall through to. The exclusion
// has to be computed ONCE, across every tier, before tiering starts (see
// deck-slots.ts's `free`/`candidates` split), not folded into the
// least-recently-reserved tiebreak that only fires with two-plus candidates.
describe('a pending reservation is excluded even as a tier singleton', () => {
	it('an empty singleton that is still pending falls through to a merely stopped deck', async () => {
		const { pickDoubleClickDeck } = await _mod();
		// CH1 is the ONLY empty deck, reserved a moment ago for a load that has
		// not settled (reservationPending true, stable_id still null). Nothing
		// else is in the empty tier, so a within-tier tiebreak alone can never
		// see CH2 - the exclusion must happen before tiering runs at all.
		const out = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: decksOf({
				1: { stable_id: null, loadSeq: 1, reservationPending: true },
				2: { stable_id: 'b', playing: false },
				3: { stable_id: 'c', playing: true },
				4: { stable_id: 'd', playing: true }
			}),
			lastDoubleClickDeck: null
		});
		assert.equal(out.deck, 2, 'returned the pending empty singleton instead of falling through');
	});

	it('falls back to a pending deck rather than refusing when every deck is pending', async () => {
		const { pickDoubleClickDeck } = await _mod();
		const out = pickDoubleClickDeck({
			shift: false,
			replace: false,
			decks: decksOf({
				1: { stable_id: null, loadSeq: 1, reservationPending: true },
				2: { stable_id: null, loadSeq: 2, reservationPending: true },
				3: { stable_id: null, loadSeq: 3, reservationPending: true },
				4: { stable_id: null, loadSeq: 4, reservationPending: true }
			}),
			lastDoubleClickDeck: null
		});
		assert.notEqual(out.deck, null, 'refused the gesture entirely when everything is pending');
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3917231700
// (P1/BLOCKING): the tests above exercise the pure picker directly with
// hand-built DeckSlotState maps, which is what the picker was split out of
// BrowserPanel to allow (module docstring, deck-slots.ts:1-9) - there is no
// component-mount harness in this repo to render BrowserPanel/TrackTable
// instead (documented precedent: JobsDrawer.svelte's test header, "no
// component mount infra (no jsdom, no @testing-library)"). What those tests
// cannot see is whether BrowserPanel's glue between live engine state and the
// picker's input shape stays wired the way it claims. Pin that glue here by
// source, the same idiom jobs-drawer.test.mjs already uses for markup that
// cannot be executed: the real DOM dblclick reaches the real picker, and
// every DeckSlotState field the picker's tests rely on is read from live
// reactive state, not a literal.
describe('BrowserPanel wires the pure picker to live state, not fabricated input', () => {
	const PANEL = fileURLToPath(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
	);
	const TABLE = fileURLToPath(
		new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
	);
	const panel = readFileSync(PANEL, 'utf8');
	const table = readFileSync(TABLE, 'utf8');

	it('a real row dblclick reaches onRowDblClick, not dead code', () => {
		assert.match(table, /ondblclick=\{\(e\) => onRowDblClick\(e, row\)\}/);
	});

	it('onRowDblClick asks the wired callback for the deck, not a fabricated one', () => {
		// The double-click and Enter-on-a-row share one load path
		// (_requestLoadPlay): the dblclick hands it the event's own modifiers,
		// and the shared path hands those straight to the picker.
		const fn = table.slice(
			table.indexOf('function onRowDblClick('),
			table.indexOf('function ', table.indexOf('function onRowDblClick(') + 1)
		);
		assert.match(
			fn,
			/_requestLoadPlay\(row, \{\s*shift: event\.shiftKey,\s*replace: event\.metaKey \|\| event\.ctrlKey,/
		);
		const shared = table.slice(
			table.indexOf('function _requestLoadPlay('),
			table.indexOf('function ', table.indexOf('function _requestLoadPlay(') + 1)
		);
		assert.match(
			shared,
			/onpickdoubledeck\?\.\(row, \{\s*shift: gesture\.shift,\s*replace: gesture\.replace\s*\}\)/
		);
	});

	it('BrowserPanel wires its own pickDoubleDeck as the callback on the real TrackTable', () => {
		assert.match(panel, /<TrackTable[\s\S]{0,2000}?onpickdoubledeck=\{pickDoubleDeck\}/);
	});

	it('the shared pick target reads every DeckSlotState field from live reactive state', () => {
		const fn = panel.slice(
			panel.indexOf('function _pickDoubleDeckTarget('),
			panel.indexOf('function ', panel.indexOf('function _pickDoubleDeckTarget(') + 1)
		);
		assert.match(fn, /stable_id:\s*decks\[d\]\.stable_id/, 'stable_id is not read from live decks');
		assert.match(fn, /playing:\s*decks\[d\]\.playing/, 'playing is not read from live decks');
		assert.match(fn, /is_master:\s*decks\[d\]\.is_master/, 'is_master is not read from live decks');
		assert.match(
			fn,
			/fader:\s*mixerState\.channels\[d\]\.fader/,
			'fader is not read from the live mixer'
		);
		assert.match(
			fn,
			/loadSeq:\s*deckLoadSeq\[d\]/,
			'loadSeq is not read from the live reservation counter'
		);
		assert.match(
			fn,
			/reservationPending:\s*deckReservationPending\[d\]/,
			'reservationPending is not read from live reservation state - the cross-tier exclusion above sees nothing'
		);
		assert.match(fn, /pickDoubleClickDeck\(\{/, 'the wired glue no longer calls the tested picker');
		assert.match(
			panel.slice(panel.indexOf('function pickDoubleDeck('), panel.indexOf('function previewSeek(')),
			/const result = _pickDoubleDeckTarget\(opts\);[\s\S]*?_reserveDeckSlot\(result\.deck\)/,
			'the returned deck is not reserved synchronously - see the race this guards against above'
		);
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3919725002
// (corollary of the fix above): reservationPending must also be RELEASED on
// every path a reservation can end without stable_id/playing/fader ever
// changing - a load/unload that settles (success or error), and a
// load-confirm dialog dismissed via "No" with no load ever dispatched. A
// reservation that is never released stays wrongly excluded from every
// future pick forever, which is worse than the race it replaces.
describe('a deck reservation is released on every exit path, not just success', () => {
	const PANEL = fileURLToPath(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
	);
	const TABLE = fileURLToPath(
		new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
	);
	const panel = readFileSync(PANEL, 'utf8');
	const table = readFileSync(TABLE, 'utf8');

	it('_loadOntoDeck releases the reservation in a finally, covering refusal, error and success', () => {
		const fn = panel.slice(
			panel.indexOf('async function _loadOntoDeck('),
			panel.indexOf('\n\tfunction ', panel.indexOf('async function _loadOntoDeck('))
		);
		assert.match(
			fn,
			/finally\s*\{[\s\S]*_releaseDeckReservation\(deck, opts\.reservation\)/,
			'no finally releases the reservation'
		);
	});

	it('the load-confirm dialog\'s No button releases the reservation instead of stranding it', () => {
		assert.match(table, /onloadconfirmcancelled\?\.\(pending\.deck, pending\.reservation\)/);
	});

	it('BrowserPanel wires the cancel callback on the real TrackTable to the real release function', () => {
		assert.match(panel, /<TrackTable[\s\S]{0,2000}?onloadconfirmcancelled=\{_releaseDeckReservation\}/);
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3919837812
// (P1/BLOCKING, found on the release-on-every-exit-path fix above): releasing
// on every non-null `deck` was too broad. The explicit "Load onto deck N"
// button also passes a non-null deck it never reserved through _reserveDeckSlot
// - if a double-click confirm dialog is ALSO open on that same deck, the
// button's own _loadOntoDeck call would release the confirm's reservation out
// from under it on its way out, letting a third gesture pick that deck while
// the confirm dialog is still waiting for an answer.
describe('a reservation is only released by the call that owns it', () => {
	const PANEL = fileURLToPath(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
	);
	const TABLE = fileURLToPath(
		new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
	);
	const panel = readFileSync(PANEL, 'utf8');
	const table = readFileSync(TABLE, 'utf8');

	it('_loadOntoDeck gates the release on opts.reservation, not merely deck !== null', () => {
		const fn = panel.slice(
			panel.indexOf('async function _loadOntoDeck('),
			panel.indexOf('\n\tfunction ', panel.indexOf('async function _loadOntoDeck('))
		);
		assert.match(
			fn,
			/finally\s*\{[\s\S]*if \(deck !== null && opts\.reservation !== undefined\) \{\s*_releaseDeckReservation\(deck, opts\.reservation\);/,
			'the finally releases whenever deck is non-null, ignoring who reserved it'
		);
	});

	it('the explicit "Load onto deck N" button does not claim a reservation it never made', () => {
		const deckBtnsStart = table.indexOf('{#each DECKS as d (d)}');
		const fn = table.slice(deckBtnsStart, table.indexOf('{/each}', deckBtnsStart));
		assert.match(fn, /onloadrow\(row, d\)/, 'button call site changed shape');
		assert.equal(
			/onloadrow\(row, d,\s*\{[^}]*reservation/.test(fn),
			false,
			'the explicit deck button now claims a reservation it never made via pickDoubleDeck'
		);
	});

	it('the double-click immediate-play path only forwards a reservation when onpickdoubledeck actually reserved the deck', () => {
		const fn = table.slice(
			table.indexOf('function onRowDblClick('),
			table.indexOf('function hl(')
		);
		assert.match(fn, /const reservation = picked != null \? picked\.reservation : null;/);
		assert.match(
			fn,
			/reservation !== null\s*\? \{ play: true, reservation, pressT0Ms: gesture\.timeStamp \}\s*: \{ play: true, pressT0Ms: gesture\.timeStamp \}/
		);
	});

	it('the confirm dialog carries the reservation generation through to both Yes and the overwrite-release guard', () => {
		assert.match(
			table,
			/pending\.reservation !== null\s*\? \{ play: true, reservation: pending\.reservation, pressT0Ms: e\.timeStamp \}\s*: \{ play: true, pressT0Ms: e\.timeStamp \}/,
			'Yes button does not forward it'
		);
		assert.match(
			table,
			/if \(loadConfirm !== null && loadConfirm\.reservation !== null\) \{\s*onloadconfirmcancelled\?\.\(loadConfirm\.deck, loadConfirm\.reservation\);/,
			'a second double-click does not release the confirm it is about to overwrite'
		);
	});

	it('loadSuggest forwards its picker-sourced generation, the same as the double-click path', () => {
		const fn = panel.slice(
			panel.indexOf('function loadSuggest('),
			panel.indexOf('\n\tconst playlistMemberIds')
		);
		assert.match(
			fn,
			/loadRow\(row, picked\.deck, \{\s*play: true,\s*reservation: picked\.reservation,\s*\.\.\.\(opts\.pressT0Ms === undefined \? \{\} : \{ pressT0Ms: opts\.pressT0Ms \}\)\s*\}\);/
		);
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3919940009
// (P1/BLOCKING): the master exclusion was only checked once, at pickDoubleDeck
// PICK time. The actual destructive load can happen much later - after a
// confirm dialog is answered - during which the picked deck can become
// master. A stale pick-time check cannot see that.
describe('the master deck is revalidated at the load boundary, not just at pick time', () => {
	const PANEL = fileURLToPath(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
	);
	const panel = readFileSync(PANEL, 'utf8');

	it('_loadOntoDeck refuses to load onto a deck that is currently master, before dispatching any command', () => {
		const fn = panel.slice(
			panel.indexOf('async function _loadOntoDeck('),
			panel.indexOf('\n\tfunction ', panel.indexOf('async function _loadOntoDeck('))
		);
		const refusalIdx = fn.search(/if \(decks\[target\]\.is_master\) \{/);
		assert.notEqual(refusalIdx, -1, 'no revalidation of is_master at the load boundary');
		const dispatchIdx = fn.indexOf('dispatchPerformanceCommand({ type: \'unload\'');
		assert.notEqual(dispatchIdx, -1, 'unload dispatch call site changed shape');
		assert.ok(
			refusalIdx < dispatchIdx,
			'the master check must run before any destructive dispatch, not after'
		);
	});

	it('the master refusal reads live decks[target] state, not a value captured at pick time', () => {
		const fn = panel.slice(
			panel.indexOf('async function _loadOntoDeck('),
			panel.indexOf('\n\tfunction ', panel.indexOf('async function _loadOntoDeck('))
		);
		assert.match(
			fn,
			/const target = deck \?\? _lowestFreeDeck\(\);[\s\S]*if \(decks\[target\]\.is_master\)/,
			'target is resolved and re-read from live decks[] before the master check, not passed in stale'
		);
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3919940011
// (P1/BLOCKING): deckReservationPending is a plain per-deck boolean, so it
// cannot distinguish which specific reservation attempt currently owns the
// pending flag once the all-pending fallback lets a later double-click
// re-reserve an already-pending deck. Two in-flight owners of one deck means
// the earlier owner's release can clear the flag while the later owner is
// still loading. Fixed with a dedicated deckReservationGen counter, not the
// shared deckLoadSeq: reusing deckLoadSeq collided with its OTHER job
// (recency bump on load success), which meant a successful load's own
// reservation could never release (r3920224748) - see the next describe.
describe('a reservation release is scoped to the generation that owns it', () => {
	const PANEL = fileURLToPath(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
	);
	const panel = readFileSync(PANEL, 'utf8');

	it('_reserveDeckSlot returns a dedicated reservation generation, not the load-recency counter', () => {
		const fn = panel.slice(
			panel.indexOf('function _reserveDeckSlot('),
			panel.indexOf('\n\t}', panel.indexOf('function _reserveDeckSlot(')) + 3
		);
		assert.match(fn, /function _reserveDeckSlot\(deck: DeckId\): number \{/);
		assert.match(fn, /deckReservationTick \+= 1;/);
		assert.match(
			fn,
			/deckReservationGen = \{ \.\.\.deckReservationGen, \[deck\]: deckReservationTick \};/
		);
		assert.match(fn, /return deckReservationTick;/);
	});

	it('_releaseDeckReservation only clears the pending flag if no newer reservation has superseded it', () => {
		const fn = panel.slice(
			panel.indexOf('function _releaseDeckReservation('),
			panel.indexOf('\n\t}', panel.indexOf('function _releaseDeckReservation(')) + 3
		);
		assert.match(fn, /function _releaseDeckReservation\(deck: DeckId, generation: number\): void \{/);
		assert.match(
			fn,
			/if \(deckReservationGen\[deck\] !== generation\) return;/,
			'release is not gated on the reservation still being the current generation'
		);
	});

	it('pickDoubleDeck threads the generation returned by _reserveDeckSlot back to its caller', () => {
		const fn = panel.slice(
			panel.indexOf('function pickDoubleDeck('),
			panel.indexOf('\n\tfunction previewSeek(')
		);
		assert.match(fn, /const reservation = _reserveDeckSlot\(result\.deck\);/);
		assert.match(fn, /return \{ deck: result\.deck, reservation \};/);
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3920224748
// (P1/BLOCKING, found on the generation-token fix above): _loadOntoDeck bumps
// deckLoadSeq[target] again on load SUCCESS (existing recency-for-the-picker
// behavior), which runs BEFORE the finally releases the reservation. Gating
// release on deckLoadSeq meant that bump always invalidated the very
// reservation whose load just succeeded, so deckReservationPending never
// cleared after a successful picker-driven load.
describe('a successful load does not invalidate its own reservation release', () => {
	const PANEL = fileURLToPath(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
	);
	const panel = readFileSync(PANEL, 'utf8');

	it('the load-success recency bump still touches deckLoadSeq, not deckReservationGen', () => {
		const fn = panel.slice(
			panel.indexOf('async function _loadOntoDeck('),
			panel.indexOf('\n\tfunction ', panel.indexOf('async function _loadOntoDeck('))
		);
		assert.match(
			fn,
			/await dispatchPerformanceCommand\(\{[\s\S]{0,20}type: 'load'[\s\S]{0,250}?deckLoadSeq = \{ \.\.\.deckLoadSeq, \[target\]: deckLoadTick \};/,
			'load success still bumps deckLoadSeq for picker recency'
		);
		const loadDispatchIdx = fn.indexOf("await dispatchPerformanceCommand({\n\t\t\t\t\ttype: 'load',");
		assert.notEqual(loadDispatchIdx, -1, 'load dispatch call site changed shape');
		const successBlock = fn.slice(loadDispatchIdx, fn.indexOf('} catch (error: unknown)'));
		assert.equal(
			/deckReservationGen/.test(successBlock),
			false,
			'the load-success path must not touch deckReservationGen - only _reserveDeckSlot may'
		);
	});

	it('_reserveDeckSlot and the load-success bump write to two different counters', () => {
		const reserveFn = panel.slice(
			panel.indexOf('function _reserveDeckSlot('),
			panel.indexOf('\n\t}', panel.indexOf('function _reserveDeckSlot(')) + 3
		);
		assert.match(reserveFn, /deckReservationGen = \{ \.\.\.deckReservationGen, \[deck\]: deckReservationTick \};/);
		assert.match(reserveFn, /deckLoadSeq = \{ \.\.\.deckLoadSeq, \[deck\]: deckLoadTick \};/);
	});
});

// ---------------------------------------------------------------------------
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3920224754
// (P1/BLOCKING): the master-exclusion recheck (r3919940009) ran at the UI
// dispatch boundary, still outside the scheduler's per-deck ordering. A
// 'master' command sharing this deck's scope ([deck, 'sync']) can be queued
// moments earlier and complete between that check and this command's own
// turn, making the deck master out from under a check that already passed.
// Fixed by rechecking inside _execute itself, using the engine's live
// getDeckState - authoritative once this command's scope has been acquired,
// since any earlier same-scope command is guaranteed to have already settled.
//
// Review thread https://github.com/maintainer/music-dj-tools/pull/945#discussion_r3920297846
// (P1/BLOCKING, found on the fix above): making that recheck unconditional
// broke every standalone unload - Deck.svelte's Unload button, Quick Draw's
// unload action - which engine.unload explicitly supports even when the
// deck is master (it elects another playing master afterward), including
// when it is the only loaded deck and there is nothing to reassign to
// first. Fixed with an opt-in `refuseIfMaster` field: only BrowserPanel's
// destructive-replace path (_loadOntoDeck) sets it; every other caller of
// 'load'/'unload' is unaffected.
describe('the master deck is rechecked inside the queued command execution, opt-in only', () => {
	const IPC = fileURLToPath(new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url));
	const ipc = readFileSync(IPC, 'utf8');
	const PANEL = fileURLToPath(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url)
	);
	const panel = readFileSync(PANEL, 'utf8');

	it("_execute's 'load' branch refuses a live-master deck only when refuseIfMaster is set", () => {
		const fn = ipc.slice(ipc.indexOf("if (command.type === 'load') {"), ipc.indexOf('await engine.load('));
		assert.match(
			fn,
			/if \(command\.refuseIfMaster === true && getDeckState\(command\.deck\)\.is_master\) \{\s*throw new Error/,
			'load does not gate its recheck on refuseIfMaster'
		);
	});

	it("_execute's 'unload' branch refuses a live-master deck only when refuseIfMaster is set", () => {
		const fn = ipc.slice(
			ipc.indexOf("} else if (command.type === 'unload') {"),
			ipc.indexOf('await engine.unload(')
		);
		assert.match(
			fn,
			/if \(command\.refuseIfMaster === true && getDeckState\(command\.deck\)\.is_master\) \{\s*throw new Error/,
			'unload does not gate its recheck on refuseIfMaster'
		);
	});

	it('the PerformanceCommand wire type accepts refuseIfMaster as optional on load and unload', () => {
		assert.match(
			ipc,
			// #3975 appended `stems?`, #4038 `suppressCommandErrorToast?` (now multi-line); refuseIfMaster must stay optional.
			/\{\s*type: 'load';\s*deck: DeckId;\s*stable_id: string;\s*refuseIfMaster\?: boolean;/
		);
		assert.match(ipc, /\{ type: 'unload'; deck: DeckId; refuseIfMaster\?: boolean \}/);
	});

	it('_loadOntoDeck opts in to refuseIfMaster on both the replace-unload and the load', () => {
		const fn = panel.slice(
			panel.indexOf('async function _loadOntoDeck('),
			panel.indexOf('\n\tfunction ', panel.indexOf('async function _loadOntoDeck('))
		);
		assert.match(
			fn,
			/dispatchPerformanceCommand\(\{ type: 'unload', deck: target, refuseIfMaster: true \}\)/
		);
		assert.match(
			fn,
			/dispatchPerformanceCommand\(\{\s*type: 'load',\s*deck: target,\s*stable_id: row\.stable_id,\s*refuseIfMaster: true\s*\}\)/
		);
	});

	it('Deck.svelte\'s standalone Unload button does not opt in, so it can still eject a live master', () => {
		const DECK = fileURLToPath(new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url));
		const deckSrc = readFileSync(DECK, 'utf8');
		const fn = deckSrc.slice(
			deckSrc.indexOf('async function unloadDeck('),
			deckSrc.indexOf('\n\t}', deckSrc.indexOf('async function unloadDeck(')) + 3
		);
		assert.match(fn, /runPerformanceCommandFromUi\(\{ type: 'unload', deck: deckId \}\)/);
		assert.equal(/refuseIfMaster/.test(fn), false, 'the standalone eject must not opt in');
	});

	it('unload and load share the deck scope with master, so they are serialized against a queued master command', () => {
		const fn = ipc.slice(
			ipc.indexOf('export function performanceCommandQueueScopes('),
			ipc.indexOf('\n}', ipc.indexOf('export function performanceCommandQueueScopes('))
		);
		assert.match(
			fn,
			/command\.type === 'master'[\s\S]*?return \[deck, 'sync'\];/,
			"master's scope no longer includes the plain deck scope 'load'/'unload' fall through to"
		);
	});
});

// ---------------------------------------------------------------------------
// P1 BLOCKING r3974057968: load-spanning press classification registry
// ---------------------------------------------------------------------------
describe('a marked press stamp is claimable exactly once', () => {
	it('an unmarked stamp claims false', async () => {
		const slots = await _mod();
		assert.equal(slots.claimLoadSpanningPress(111.1), false);
	});

	it('claim is undefined-safe, so callers need not guard a missing stamp', async () => {
		const slots = await _mod();
		assert.equal(slots.claimLoadSpanningPress(undefined), false);
	});

	it('a marked stamp claims true once, then false - the mark does not leak to a later press', async () => {
		const slots = await _mod();
		slots.markLoadSpanningPress(222.2);
		assert.equal(slots.claimLoadSpanningPress(222.2), true);
		assert.equal(
			slots.claimLoadSpanningPress(222.2),
			false,
			'a second schedule reusing the same float must not inherit the first one\'s classification'
		);
	});

	it('marking one stamp does not classify a different one', async () => {
		const slots = await _mod();
		slots.markLoadSpanningPress(333.3);
		assert.equal(slots.claimLoadSpanningPress(444.4), false);
		// The original mark is still there, unaffected by the miss above.
		assert.equal(slots.claimLoadSpanningPress(333.3), true);
	});
});

// ---------------------------------------------------------------------------
// P1 BLOCKING PRRT_kwDOSEvNd86g96Le: armed hot-cue press classification registry
// ---------------------------------------------------------------------------
describe('a marked armed-hot-cue stamp is claimable exactly once, independent of the load-span registry', () => {
	it('an unmarked stamp claims false', async () => {
		const slots = await _mod();
		assert.equal(slots.claimArmedHotCuePress(555.5), false);
	});

	it('claim is undefined-safe, so callers need not guard a missing stamp', async () => {
		const slots = await _mod();
		assert.equal(slots.claimArmedHotCuePress(undefined), false);
	});

	it('a marked stamp claims true once, then false - the mark does not leak to a later press', async () => {
		const slots = await _mod();
		slots.markArmedHotCuePress(666.6);
		assert.equal(slots.claimArmedHotCuePress(666.6), true);
		assert.equal(
			slots.claimArmedHotCuePress(666.6),
			false,
			'a second schedule reusing the same float must not inherit the first one\'s classification'
		);
	});

	it('marking an armed stamp does not classify it as load-spanning, or vice versa', async () => {
		const slots = await _mod();
		slots.markArmedHotCuePress(777.7);
		assert.equal(slots.claimLoadSpanningPress(777.7), false);
		assert.equal(slots.claimArmedHotCuePress(777.7), true);
		slots.markLoadSpanningPress(888.8);
		assert.equal(slots.claimArmedHotCuePress(888.8), false);
		assert.equal(slots.claimLoadSpanningPress(888.8), true);
	});
});
