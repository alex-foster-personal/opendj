<script lang="ts" module>
	import { readKeyNudgeStep, subscribeKeyNudgeStep, type KeyNudgeStep } from '$lib/player/key/camelot';

	// DECKGAP-7691b: one step size for every deck, owned by camelot.ts and set
	// only through the `key_nudge_step` command; this mirror redraws the row.
	let keyNudgeStep: KeyNudgeStep = $state(readKeyNudgeStep());
	subscribeKeyNudgeStep((step) => {
		keyNudgeStep = step;
	});
</script>

<script lang="ts">
	import { untrack } from 'svelte';
	import { truncationTip } from '$lib/ui/truncation-tip';
	import { shownKey } from '$lib/rb/key-notation';
	// Deck header row (SCREENSHOT-SPEC 3, COMPONENT-MAP 1.3): artwork thumb,
	// deck number, title/artist, BPM+KEY readout, remaining/elapsed clocks,
	// KEY SYNC with the key readout and nudge arrows stacked under it,
	// BEAT SYNC and exclusive MASTER stacked at the right.
	import { deckArtworkUrl } from '$lib/rb/artwork-url';
	import { coverFailed, coverLoaded, coverSrc, coverVisible } from '$lib/rb/artwork-cover.svelte';
	import { offloadOps } from '$lib/offload/offload-ops.svelte';
	import { operationPercent } from '$lib/offload/api-offload';
	import { camelotKeyColor, camelotKeyHoverLabel } from '$lib/rb/camelot-color';
	import { formatTempoPct } from '$lib/rb/tempo-format';
	import { formatKeySyncDeltaText, keySyncNotationBullet } from '$lib/rb/key-sync-copy';
	import {
		DECK_IDS,
		deckEffectiveBpm,
		deckStates,
		keyShiftOptions,
		keySyncAudibleCamelotKey,
		keySyncPreview,
		keySyncStatus,
		pitchRanges,
		rateDeckTrack
	} from '$lib/rb/audio-engine.svelte';
	import { tempoBoundsFromPitchRange } from '$lib/rb/auto-play';
	import { beatSyncGridWarning, playbackBpm } from '$lib/rb/beat-sync-math';
	import { deckGridWarningText, gridAudioFitFor, requestGridAudioFit } from '$lib/rb/grid-audio-fit.svelte';
	import { queryMasterMode, runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import { MASTER_LOCK_LONG_PRESS_MS } from '$lib/rb/master-election';
	import { gridFeatureInertTip, gridFeaturesInert } from '$lib/player/grid-features';
	import { deckKeyAtPlayheadNow } from '$lib/player/key-playhead-lazy.svelte';
	import { keyNudgeSemitones, keySourceHint, keySyncKey, keySyncKeyIsGuess, keySyncStatusTitle, type KeySyncMode, type KeySyncStatus } from '$lib/player/key/camelot';
	import type { DeckId } from '$lib/rb/deck-slots';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import ControlExplainer from './ControlExplainer.svelte';
	import { TOOLTIP_DECK_CHROME, TOOLTIP_ISOLATED } from '$lib/ui/tooltip-timing';
	import BpmDisputeMarker from '../BpmDisputeMarker.svelte';
	import { bpmDisputeText, otherAlternative, useAlternativeLabel } from '$lib/rb/bpm-dispute';
	import { putBpmPick } from '$lib/rb/api-rb';
	import { pushToast } from '$lib/stores.svelte';
	import RatingStars from '../browser/RatingStars.svelte';
	import ContextMenu from '../ContextMenu.svelte';
	import type { BoundMenuItem as ContextMenuItem } from '$lib/context-menu/bind';
	import { DECK_MOVE_DEFAULT_CROSSFADE_BEATS, deckMoveMenuEntries } from '$lib/rb/deck-move';

	let {
		deck,
		deckId,
		pending,
		onBeatSync,
		onMaster,
		onMasterTempo,
		onKeySync,
		onKeyNudge,
		onResetTempo,
		onUnload,
		keySyncAvailable,
		mirrored
	}: {
		deck: DeckState;
		deckId: DeckId;
		pending: boolean;
		onBeatSync: () => Promise<void>;
		onMaster: (lockGesture: boolean) => Promise<void>;
		onMasterTempo: () => Promise<void>;
		onKeySync: () => Promise<void>;
		onKeyNudge: (semitones: number) => Promise<void>;
		onResetTempo: (ratio: number) => Promise<void>;
		onUnload: () => Promise<void>;
		keySyncAvailable: boolean;
		/** DECKUX-8360: true horizontal mirror (art outside, sync inside). */
		mirrored: boolean;
	} = $props();

	let tempoEditAt: { x: number; y: number } | null = $state(null);
	const artSrc: string | null = $derived(
		deck.stable_id === null ? null : deckArtworkUrl(deck.stable_id, 'orig')
	);

	// DECKGAP-7691d (Preview pin 65b1138bd823): the large figure is the jog wheel's live BPM
	// (grid interval x pitch). deck.bpm is the analysed original and stays
	// in the "(from N bpm)" line, so a pitched deck does not read 122 from 122.
	const liveBpm: number | null = $derived(
		playbackBpm({
			beats: deck.anlz?.beatgrid.beats,
			positionSec: Math.max(0, deck.position_ms / 1000),
			tempoRatio: deck.pitch,
			tagBpm: deck.bpm
		})
	);
	const bpmText: string = $derived(liveBpm === null ? '--.--' : liveBpm.toFixed(2));
	// GRID-OCTAVE-02: the same marker and line the library BPM cell shows; the
	// readout's existing explainer card carries the line and the other tempo.
	const bpmDisputed: boolean = $derived(deck.anlz?.bpm_disputed === true);
	const bpmAlternatives = $derived(deck.anlz?.bpm_alternatives ?? []);
	const bpmDisputeLine: string | null = $derived(
		bpmDisputed ? bpmDisputeText(bpmAlternatives, deck.anlz?.beatgrid.bpm ?? null, deck.anlz?.beatgrid.bpm_confidence) : null
	);
	const otherTempo = $derived(bpmDisputed ? otherAlternative(bpmAlternatives, deck.anlz?.beatgrid.bpm ?? null) : null);
	let pickingTempo = $state(false);

	async function useOtherTempo(): Promise<void> {
		const id = deck.stable_id;
		if (otherTempo === null || id === null || pickingTempo) return;
		pickingTempo = true;
		try {
			// The server announces beatgrid_edit, so this deck re-reads its grid.
			await putBpmPick(id, otherTempo.source);
		} catch (err) {
			pushToast(`Could not switch the tempo: ${err instanceof Error ? err.message : String(err)}`, 'error');
		} finally {
			pickingTempo = false;
		}
	}
	const bpmTitle = 'Live BPM: the beat grid tempo at the playhead times this deck\'s pitch, the same figure the jog wheel shows';
	const syncBounds = $derived(tempoBoundsFromPitchRange(pitchRanges[deckId]));
	// Beat Sync is inert when gridFeaturesInert is true (missing / failed /
	// static_grid_untrusted: true). A trusted multi-anchor own map is not
	// gridless; Q and Beat Sync stay live. Transport is still not gated.
	const gridless: boolean = $derived(gridFeaturesInert(deck));
	const gridInertTip: string = $derived(gridFeatureInertTip(deck));
	// BEATSYNC-GRID-02: an uneven or extrapolated grid is said out loud, on
	// the control it affects, before the DJ leans on the lock. Pure math over
	// the deck's real grid; null for a clean grid and for no grid at all.
	const gridShapeWarning: string | null = $derived(
		gridless ? null : (beatSyncGridWarning(deck.anlz?.beatgrid.beats ?? [])?.message ?? null)
	);
	// GRIDFIT-03: whether the grid matches the audio is judged by the engine,
	// asked once per successful verdict per page session and only after the
	// grid has arrived (never during the load itself); a failed ask retries
	// on the next load of that id. Cleanup cancels the delayed ask when this
	// deck changes track or unloads. Keyed on stable_id plus a boolean so an
	// anlz object replacement (grid edit, phase refine, bpm pick) does not
	// restart the delay.
	// untrack: requestGridAudioFit reads and writes the reactive `_entries`
	// cache; a tracked call re-enters on pending and on failed, which loops
	// or retries without another load. The effect's real dependencies are
	// stable_id and anlz-present.
	const gridFitAnlzPresent = $derived(deck.anlz !== null);
	$effect(() => {
		const id = deck.stable_id;
		if (id === null || !gridFitAnlzPresent) return;
		return untrack(() => requestGridAudioFit(id));
	});
	const gridWarning: string | null = $derived(
		gridless ? null : deckGridWarningText(gridShapeWarning, gridAudioFitFor(deck.stable_id))
	);
	const beatSyncTitle: string = $derived(
		(gridless
			? gridInertTip
			: deck.beat_sync_enabled
				? 'BEAT SYNC ON - lock beat phase to the tempo MASTER (BAR phase, folding if it must)'
				: 'BEAT SYNC OFF - this deck keeps its own tempo and phase') +
			(gridWarning === null ? '' : ` | ${gridWarning}`)
	);
	const beatSyncBullets: readonly string[] = $derived([
		'Locks this deck to the MASTER beat grid (BAR: beat 1 aligns with 1, … 4 with 4 whenever the two tempos match outright).',
		`Needs a real PQTZ grid on both decks. BAR tempo must land in pitch range [${syncBounds.min}, ${syncBounds.max}] (default +-16%).`,
		'Outside that window sync cannot engage - button reverts; use pitch or pick a closer BPM.',
		'No grid on this track: the button is inert and transport runs unsynced.',
		'BAR prefers an exact match, and folds to half/double tempo rather than refusing when that is the only lock available - it warns in orange and stays locked.',
		'A locked deck is kept on the beat by tempo trims of at most 0.3%, and re-joined when it is more than 15 ms off. An orange ! means this track has an uneven beatgrid, or a grid whose tempo or beat does not match its audio, so the lock may wander or drift.'
	]);
	const masterMode = $derived(queryMasterMode());
	const masterTitle: string = $derived(
		deck.stable_id === null
			? 'no track loaded'
			: deck.is_master && masterMode === 'locked'
				? 'MASTER LOCKED - level handoff is off; click for AUTO, or hold (Shift/Alt-click) to unlock'
				: deck.is_master
					? 'MASTER AUTO - this deck is the tempo / key sync reference; hold or Shift/Alt-click to lock'
					: 'MASTER - make this deck the tempo / key sync reference (AUTO); hold or Shift/Alt-click to lock'
	);
	const masterBullets: readonly string[] = [
		'Only one MASTER at a time. Followers with Beat Sync lock phase to this deck.',
		'KEY SYNC also uses MASTER as the Camelot reference.',
		'BeatSyncMax keeps BAR phase lock across seeks when followers are synced.',
		'AUTO hands off when this deck stays below -30 dB (after EQ, filter and stems) for a beat while another playing deck is above -20 dB.',
		'Click picks MASTER and keeps AUTO. Hold it, or Shift/Alt-click, to LOCK: no level handoff.',
		'Stopping, playing out or unloading always hands off, even when LOCKED, and drops the lock.'
	];
	// Long-press locks (MASTER_LOCK_LONG_PRESS_MS); the click that ends a hold is swallowed.
	let masterHold: ReturnType<typeof setTimeout> | null = null;
	let masterHoldFired = false;
	function startMasterHold(event: PointerEvent): void {
		masterHoldFired = false;
		if (!event.isPrimary || event.button !== 0) return; // only the primary press locks
		cancelMasterHold();
		masterHold = setTimeout(() => {
			masterHold = null;
			masterHoldFired = true;
			void onMaster(true);
		}, MASTER_LOCK_LONG_PRESS_MS);
	}
	function cancelMasterHold(): void {
		if (masterHold !== null) clearTimeout(masterHold);
		masterHold = null;
	}
	async function onMasterClick(event: MouseEvent): Promise<void> {
		cancelMasterHold();
		if (masterHoldFired) {
			masterHoldFired = false;
			return;
		}
		await onMaster(event.shiftKey || event.altKey);
	}
	const masterDataState = $derived(
		deck.is_master ? (masterMode === 'locked' ? 'locked' : 'auto') : 'off'
	);
	const masterAriaLabel = $derived(
		deck.is_master
			? masterMode === 'locked'
				? `master deck ${deckId} locked`
				: `master deck ${deckId} auto`
			: `master deck ${deckId}`
	);
	const keySyncPlan = $derived.by(() => {
		const source = deckStates[deckId];
		void source.key;
		void source.key_shift_semitones;
		void source.pitch;
		void source.master_tempo_enabled;
		void source.transport_pending;
		void source.key_sync_mode;
		void source.key_sync_target;
		void source.key_guess;
		const masterId = source.key_sync_target ?? DECK_IDS.find((candidate) => deckStates[candidate].is_master);
		if (masterId !== undefined) {
			const master = deckStates[masterId];
			void master.key;
			void master.key_shift_semitones;
			void master.pitch;
			void master.master_tempo_enabled;
			void master.transport_pending;
		}
		return keySyncPreview(deckId);
	});
	/** DECK-KEY-01: the library's resolved key (else its "?" guess), the one key every deck surface reads. */
	const deckKey: string | null = $derived(keySyncKey(deck));
	/** Show audible Camelot after KEY SYNC / nudge; playhead segments override metadata. */
	const keyPlayhead = $derived(deckKeyAtPlayheadNow(deck));
	const keyText: string = $derived(keyPlayhead.display ?? '--');
	/** keyText in the ONE key notation the user picked (ui-prefs key_notation). */
	const keyShown: string = $derived(keyText === '--' ? '--' : (shownKey(keyText) ?? keyText));
	const origKeyShown: string | null = $derived(shownKey(deckKey));
	/** KEY-GUESS-01: no key, but the analyzer's best guess, shown dimmed with "?". */
	const keyGuessShown: string | null = $derived(keyPlayhead.isGuess && keyText !== '--' ? `${keyShown}?` : null);
	const masterEffectiveKey: string | null = $derived.by(() => {
		if (keySyncPlan === null) return null;
		const master = deckStates[keySyncPlan.masterDeck];
		return keySyncAudibleCamelotKey(
			keySyncKey(master),
			master.pitch,
			master.master_tempo_enabled,
			master.key_shift_semitones
		);
	});
	const followerEffectiveKey: string | null = $derived(keyText === '--' ? null : keyText);
	const keySyncDeltaText: string | null = $derived.by(() => {
		if (keySyncPlan === null) return null;
		return formatKeySyncDeltaText(
			followerEffectiveKey,
			masterEffectiveKey,
			keySyncPlan.deltaSemitones
		);
	});
	// Lit only while the follow really holds (DECKUX-34): an arm that is
	// waiting on a master reads as ARMED, never as ON over a wrong key.
	// keySyncStatus also reads the engine's presented plan (not $state), so
	// re-derive on the same published fields keySyncPlan tracks above.
	const keySyncState: KeySyncStatus = $derived.by(() => {
		void keySyncPlan;
		for (const candidate of DECK_IDS) {
			void deckStates[candidate].is_master;
			void deckStates[candidate].stable_id;
		}
		void deck.key_sync_enabled;
		void deck.key_sync_mode;
		void deck.key_sync_target;
		return keySyncStatus(deckId);
	});
	const keySyncTitle: string = $derived(
		keySyncState === 'following'
			? `KEY SYNC ON (${deck.key_sync_mode}) - this deck follows ${deck.key_sync_target === null ? 'the MASTER' : `deck ${deck.key_sync_target}`} key${keySyncKeyIsGuess(deck) || (keySyncPlan !== null && keySyncKeyIsGuess(deckStates[keySyncPlan.masterDeck])) ? ' - WARNING: uses a guessed "?" key' : ''}`
			: keySyncState !== 'off'
				? keySyncStatusTitle(keySyncState)
				: !keySyncAvailable
					? 'requires a loaded Camelot-key master'
					: keySyncDeltaText === null
						? 'KEY SYNC OFF - exact target is unavailable'
						: `KEY SYNC OFF - ${keySyncDeltaText}`
	);
	const keySyncBullets: readonly string[] = $derived.by(() => {
		if (keySyncPlan === null) {
			return ['Load a parseable Camelot-key master and wait for the presented audio state.'];
		}
		const bullets = [
			`Target manual shift: ${keySyncPlan.targetManualShiftSemitones >= 0 ? '+' : ''}${keySyncPlan.targetManualShiftSemitones} semitones.`,
			'Click KEY SYNC to apply this exact listener-facing target.'
		];
		const notation = keySyncNotationBullet(followerEffectiveKey, masterEffectiveKey);
		if (notation !== null) bullets.push(notation);
		return bullets;
	});
	const keySyncWarning: string | null = $derived(
		!deck.master_tempo_enabled
			? 'Master Tempo is OFF. Tempo changes affect vocal pitch; KEY SYNC itself does not change playback speed.'
			: null
	);
	const keyColor: string | null = $derived(camelotKeyColor(keyText === '--' ? null : keyText));
	const keyHover: string | null = $derived.by(() => {
		const effective = keyText === '--' ? null : keyText;
		const crossNotation = camelotKeyHoverLabel(effective);
		const segmentNote = keyPlayhead.title;
		let base: string | null;
		if (segmentNote !== null) {
			base =
				crossNotation === null ? segmentNote : `${crossNotation} — ${segmentNote}`;
		} else {
			base = crossNotation;
		}
		const hint = keySourceHint(deck.key_source);
		if (hint !== null && base !== null) base = `${base} (${hint})`;
		const raw = deckKey;
		if (raw === null || deck.key_shift_semitones === 0) return base;
		const shift =
			deck.key_shift_semitones >= 0
				? `+${deck.key_shift_semitones}`
				: String(deck.key_shift_semitones);
		const suffix = ` (was ${shownKey(raw) ?? raw}, shift ${shift})`;
		return base === null ? `${effective}${suffix}` : `${base}${suffix}`;
	});
	const origKeyHover: string | null = $derived(camelotKeyHoverLabel(deckKey));
	const keyShiftText: string = $derived(
		deck.key_shift_semitones >= 0
			? `+${deck.key_shift_semitones}`
			: String(deck.key_shift_semitones)
	);

	// ----------------------------------------- KEY SYNC ladder (KEYSYNC-01)
	// The chevron opens a vertical ladder: +6 at the top, 0 level with the
	// button, -6 at the bottom. Each row is the key it lands on against the
	// target's audible key, thumbs-up + Mixed In Key pattern when it mixes.
	// Picking a row sends key_shift (key lock on, KEY SYNC off); the target
	// and mode chips re-send key_sync with the deck's current arm.
	let ladderAt: { x: number; y: number } | null = $state(null);
	let ladderEl: HTMLDivElement | null = $state(null);
	$effect(() => {
		ladderEl?.focus(); // Esc reaches the ladder's own keydown
	});
	const ladderTargetDeck: DeckId | null = $derived(
		deck.key_sync_target ?? DECK_IDS.find((id) => id !== deckId && deckStates[id].is_master) ?? null
	);
	const ladderRows = $derived.by(() => {
		if (ladderAt === null || ladderTargetDeck === null || ladderTargetDeck === deckId) return null;
		const target = deckStates[ladderTargetDeck];
		void target.key;
		void target.key_shift_semitones;
		void target.pitch;
		void target.master_tempo_enabled;
		void target.stable_id;
		void deck.key;
		return keyShiftOptions(deckId, ladderTargetDeck);
	});
	const KEY_SYNC_MODE_CHIPS: readonly { id: KeySyncMode; label: string; title: string }[] = [
		{ id: 'exact', label: 'exact', title: "Exact: transpose onto the target's key, shortest path -6..+6 (Serato Pitch 'n Time Key Sync)." },
		{ id: 'relative', label: 'rel', title: "Relative: land on the target's Camelot number in this deck's mode (A<->B, the same notes)." },
		{ id: 'compatible', label: 'compat', title: 'Compatible: nearest Camelot neighbour of the target (Pioneer / rekordbox Key Sync).' }
	];
	function openLadder(event: MouseEvent): void {
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		ladderAt = ladderAt === null ? { x: rect.right + 2, y: rect.top + rect.height / 2 } : null;
	}
	function closeLadder(): void {
		ladderAt = null;
	}
	async function pickShift(shift: number): Promise<void> {
		closeLadder();
		await runPerformanceCommandFromUi({ type: 'key_shift', deck: deckId, semitones: shift });
	}
	async function pickKeySyncSetting(setting: { mode?: KeySyncMode; target?: DeckId | 'master' }): Promise<void> {
		await runPerformanceCommandFromUi({ type: 'key_sync', deck: deckId, enabled: deck.key_sync_enabled, ...setting });
	}
	async function resetKeyShift(): Promise<void> {
		if (deck.stable_id === null || deck.key_shift_semitones === 0) return;
		await runPerformanceCommandFromUi({ type: 'key_shift', deck: deckId, semitones: 0 });
	}
	function onLadderKey(event: KeyboardEvent): void {
		if (ladderAt !== null && event.key === 'Escape') {
			event.stopPropagation();
			closeLadder();
		}
	}

	// Pin 783ce03c0dbd. Standard DJ key shift is one semitone, so that stays
	// the selected step. Tone, scale, and one Camelot (a fifth) are the other
	// sizes, picked here and applied as one nudge, not a run of semitones.
	const KEY_NUDGE_STEPS: readonly { id: KeyNudgeStep; label: string; title: string }[] = [
		{
			id: 'semitone',
			label: 'semi',
			title: 'One semitone. Standard DJ key shift (CDJ, rekordbox, Serato, Traktor).'
		},
		{ id: 'tone', label: 'tone', title: 'One whole tone (two semitones).' },
		{ id: 'scale', label: 'scale', title: "Next note in this track's scale." },
		{ id: 'camelot', label: '5th', title: 'One Camelot step: a perfect fifth, one number around the wheel.' }
	];

	function nudgeDelta(direction: -1 | 1): number | null {
		return keyNudgeSemitones(keyNudgeStep, direction, deckKey, deck.key_shift_semitones);
	}

	function nudgeFits(direction: -1 | 1): boolean {
		const delta = nudgeDelta(direction);
		if (delta === null) return false;
		const next = deck.key_shift_semitones + delta;
		return next >= -12 && next <= 12;
	}

	function nudgeTitle(direction: -1 | 1): string {
		if (keyNudgeStep === 'scale' && nudgeDelta(direction) === null) {
			return 'Scale step needs a Camelot or musical key on this track';
		}
		if (keyNudgeStep === 'semitone') {
			return direction < 0 ? 'lower key by one semitone' : 'raise key by one semitone';
		}
		if (keyNudgeStep === 'tone') {
			return direction < 0 ? 'lower key by one whole tone' : 'raise key by one whole tone';
		}
		if (keyNudgeStep === 'scale') {
			return direction < 0
				? "lower key to the previous note in this track's scale"
				: "raise key to the next note in this track's scale";
		}
		return direction < 0
			? 'lower key by one Camelot step, a perfect fifth'
			: 'raise key by one Camelot step, a perfect fifth';
	}

	function nudgeAria(direction: -1 | 1): string {
		return `${nudgeTitle(direction)} deck ${deckId}`;
	}

	async function nudge(direction: -1 | 1): Promise<void> {
		const delta = nudgeDelta(direction);
		if (delta === null || !nudgeFits(direction)) return;
		await onKeyNudge(delta);
	}

	// --------------------------------------- tempo/key readout (pin 815937c87bc1)
	// Original-key colour is deck.key's OWN camelot colour, distinct from
	// keyColor above which tracks the shifted (current) key.
	const origKeyColor: string | null = $derived(camelotKeyColor(deckKey));
	const keyChanged: boolean = $derived(deckKey !== null && deck.key_shift_semitones !== 0);
	const tempoChanged: boolean = $derived(deck.pitch !== 1);
	// DECKUX-41: the shared formatter, the same one JogDial uses ("+2%", "-2%", "±0%").
	const tempoPctText: string = $derived(formatTempoPct(deck.pitch));
	const resetTitle: string = $derived(
		deck.bpm === null
			? 'Reset to original tempo'
			: `Reset to original tempo: ${deck.bpm.toFixed(0)}bpm${origKeyShown !== null ? ` | key: ${origKeyShown}` : ''}`
	);
	const resetBullets: string[] = [
		'Tempo returns to the analysed 0% (the track\'s original bpm).',
		'Key returns to the analysed key by re-applying the existing single-semitone nudge back to 0 - there is no separate absolute key-reset command.'
	];

	/** Combined reset: tempo goes straight to ratio 1.0 (0%); key has no
	 * absolute-reset primitive in the engine, so it is walked back to 0
	 * semitones one nudge at a time through the SAME onKeyNudge the badge's
	 * arrows already use - no new IPC command type. */
	async function resetToOriginal(): Promise<void> {
		await onResetTempo(1);
		const shift = deck.key_shift_semitones;
		const step: -1 | 1 = shift > 0 ? -1 : 1;
		for (let i = 0; i < Math.abs(shift); i++) {
			await onKeyNudge(step);
		}
	}

	function onTempoReadoutDblClick(event: MouseEvent): void {
		if (deck.stable_id === null || deckEffectiveBpm(deckId) === null) return;
		event.preventDefault();
		event.stopPropagation();
		tempoEditAt = { x: event.clientX, y: event.clientY };
	}

	// ----------------------------------------------------------- _helpers

	function _fmtClock(ms: number): string {
		// MM:SS.d per SCREENSHOT-SPEC 3 (e.g. 00:00.0).
		const clamped = Math.max(0, ms);
		const totalS = clamped / 1000;
		const m = Math.floor(totalS / 60);
		const s = Math.floor(totalS % 60);
		const tenths = Math.floor((totalS * 10) % 10);
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${tenths}`;
	}

	const elapsedText: string = $derived(
		deck.duration_ms === null ? '--:--.-' : _fmtClock(deck.position_ms)
	);
	const remainText: string = $derived(
		deck.duration_ms === null ? '--:--.-' : `-${_fmtClock(deck.duration_ms - deck.position_ms)}`
	);
	const remainTitle: string = $derived(
		deck.duration_ms === null
			? 'Remaining time --:--.- (no track loaded)'
			: `Remaining time ${remainText} (MM:SS.d until the end of this track)`
	);
	const elapsedTitle: string = $derived(
		deck.duration_ms === null
			? 'Elapsed time --:--.- (no track loaded)'
			: `Elapsed time ${elapsedText} (MM:SS.d from the start of this track)`
	);
	const keyOffTitle: string = $derived.by(() => {
		const base = `Key shift ${keyShiftText} semitones from the original key`;
		const currentNote = camelotKeyHoverLabel(followerEffectiveKey);
		const originalNote = camelotKeyHoverLabel(deckKey);
		if (currentNote === null && originalNote === null) return base;
		const parts = [base];
		if (currentNote !== null) parts.push(`Current: ${currentNote}`);
		if (originalNote !== null) parts.push(`Original: ${originalNote}`);
		return parts.join(' — ');
	});
	const deckNumTitle: string = $derived(`Deck ${deckId}`);
	// DECKMOVE-01 (the maintainer, Wed 4 Nov 2026): right-click the artwork, "Move track
	// to deck N". The target takes the track, playhead, live loop and settings,
	// crossfades over 8 beats, and this deck unloads. A playing target is named
	// in the item, and picking that item is the explicit force. Same bus
	// command as `opendj deck_move`, HTTP and the MCP tool.
	let moveMenuAt: { x: number; y: number } | null = $state(null);

	const moveMenuItems: ContextMenuItem[] = $derived(
		deckMoveMenuEntries(deckId, DECK_IDS, (d) => deckStates[d]).map(({ to, label, force }) => ({
			id: `deck-move-${to}`,
			label,
			testId: `deck-${deckId}-move-to-${to}`,
			title: `Clone track, playhead, loop and settings onto deck ${to}, crossfade ${DECK_MOVE_DEFAULT_CROSSFADE_BEATS} beats, then unload deck ${deckId}`,
			run: () => void runPerformanceCommandFromUi({ type: 'deck_move', from: deckId, to, ...(force ? { force: true } : {}) })
		}))
	);

	// OFFLOAD: the track this deck is waiting on, while it downloads from the cloud.
	const cloudDownload = $derived(offloadOps.deckDownloads[deckId] ?? null);
	const cloudPercent = $derived(cloudDownload === null ? null : operationPercent(cloudDownload.op));
</script>

<div class="deck-header" class:mirrored data-mirrored={mirrored}>
	{#if cloudDownload !== null}
		<div
			class="deck-cloud-download"
			data-testid="deck-cloud-download"
			role="progressbar"
			aria-label={`Downloading from the cloud for deck ${deckId}`}
			aria-valuemin="0"
			aria-valuemax="100"
			aria-valuenow={cloudPercent ?? undefined}
			title={`Downloading from the cloud: ${cloudPercent === null ? 'starting' : `${Math.round(cloudPercent)}%`}`}
		>
			<span class="deck-cloud-fill" style={`width:${cloudPercent ?? 0}%`}></span>
		</div>
	{/if}
	<div class="art-slot">
		{#if deck.stable_id !== null}
			<button
				type="button"
				class="art-btn"
				title="Unload deck"
				aria-label={`Unload deck ${deckId}`}
				data-testid={`unload-deck-${deckId}`}
				disabled={pending}
				onclick={() => void onUnload()}
				oncontextmenu={(event) => {
					event.preventDefault();
					moveMenuAt = { x: event.clientX, y: event.clientY };
				}}
			>
				{#if artSrc !== null && deck.stable_id !== null && coverVisible(deck.stable_id)}
					{@const sid = deck.stable_id}
					<img
						class="art"
						src={coverSrc(artSrc, sid)}
						alt=""
						data-expected-resource-error="artwork-404"
						onload={() => coverLoaded(sid)}
						onerror={() => coverFailed(sid)}
					/>
				{:else}
					<span
						class="art placeholder"
						title="No artwork found: none in rekordbox, the file, a cover image in its folder, or online"
					></span>
				{/if}
				<span class="art-eject" aria-hidden="true">⏏</span>
			</button>
		{:else}
			<div class="art placeholder"></div>
		{/if}
		{#if moveMenuAt !== null && deck.stable_id !== null}
			<ContextMenu items={moveMenuItems} x={moveMenuAt.x} y={moveMenuAt.y} onclose={() => (moveMenuAt = null)} />
		{/if}
	</div>

	<!-- Body wraps beside art so a second chrome row does not stack under the
	     full artwork height (header = max(art, body), not art + body). -->
	<div class="header-body">
		<span class="deck-num" title={deckNumTitle}>{deckId}</span>

		<div class="meta" class:empty={deck.stable_id === null}>
			<!-- The title and artist ellipsize in a narrow deck; the hover tip is
			     the only way to read a long one in full (public-launch sweep, Wed 7 Oct 2026).
			     LIBUX-57: the title's tip shows only when it is cut off, after a rest. -->
			<span class="title" data-custom-tip="" use:truncationTip>{deck.title ?? 'No track loaded'}</span>
			<span class="artist" title={deck.artist || undefined}>{deck.artist ?? ''}</span>
			{#if deck.stable_id !== null}
				<span class="deck-rating-row">
					<RatingStars rating={deck.rating} onrate={(n) => void rateDeckTrack(deckId, n)} />
					<!-- Dot only, per pin scope: the library column, the click-to-set
					     swatch selector, and the shift-stacked multi-tag layout are a
					     separate feature and stay out of this packet. -->
					<span class="color-dot" title="Color tag (not yet settable)"></span>
				</span>
			{/if}
		</div>

		{#snippet resetToOriginalAction()}
			<button
				type="button"
				class="reset-to-original"
				disabled={pending || deck.stable_id === null}
				aria-label={`reset deck ${deckId} to original tempo and key`}
				data-testid={`reset-to-original-deck-${deckId}`}
				onclick={async () => await resetToOriginal()}
			>
				{resetTitle}
			</button>
			{#if otherTempo !== null}
				<button
					type="button"
					class="use-other-tempo"
					disabled={pickingTempo}
					data-testid={`use-other-tempo-deck-${deckId}`}
					onclick={async () => await useOtherTempo()}
				>
					{pickingTempo ? 'Switching...' : useAlternativeLabel(otherTempo)}
				</button>
			{/if}
		{/snippet}
		<ControlExplainer
			title={resetTitle}
			bullets={resetBullets}
			warning={bpmDisputeLine}
			action={deck.stable_id === null ? null : resetToOriginalAction}
			tooltip={TOOLTIP_DECK_CHROME}
		>
			<div class="readout">
				{#if keyChanged}
					<span class="readout-key-line">
						<span style={keyColor !== null ? `color:${keyColor}` : undefined}>{keyShown}</span>
						<span class="from">
							 (from <span
								style={origKeyColor !== null ? `color:${origKeyColor}` : undefined}
								aria-label={origKeyHover ?? undefined}
								>{origKeyShown}</span>)
						</span>
					</span>
				{/if}
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<span
					class="bpm"
					data-testid={`tempo-readout-deck-${deckId}`}
					aria-label={bpmTitle}
					ondblclick={onTempoReadoutDblClick}
				>{bpmText}</span>{#if bpmDisputed}<span class="bpm-dispute" data-testid={`bpm-dispute-marker-deck-${deckId}`} role="img" aria-label="tempo disputed with Mixed In Key"><BpmDisputeMarker /></span>{/if}
				{#if tempoChanged}
					<span class="readout-tempo-line">
						{tempoPctText} <span class="from"> (from {deck.bpm?.toFixed(0) ?? '--'}bpm)</span>
					</span>
				{/if}
			</div>
		</ControlExplainer>

		<div class="clocks">
			<span class="remain" title={remainTitle}>{remainText}</span>
			<span class="elapsed" title={elapsedTitle}>{elapsedText}</span>
		</div>

		<div class="chrome">
			{#snippet masterTempoAction()}
				<button
					type="button"
					class="enable-master-tempo"
					disabled={pending}
					aria-label={`enable master tempo deck ${deckId}`}
					data-testid={`enable-master-tempo-deck-${deckId}`}
					onclick={async () => await onMasterTempo()}
				>
					Enable MT
				</button>
			{/snippet}
			<div class="key-stack" data-key-nudge-step={keyNudgeStep}>
				<!-- Pin 59178f89e6f1: the key value and the shift sit in one
				     block directly under KEY SYNC. The step row stays below
				     that pair so it cannot stretch the value out from under
				     the button. -->
				<div class="key-pair">
					<ControlExplainer
						title={keySyncTitle}
						bullets={keySyncBullets}
						warning={keySyncWarning}
						action={keySyncWarning === null ? null : masterTempoAction}
						tooltip={TOOLTIP_ISOLATED}
					>
						<button
							class="rb-lit-button keysync"
							class:lit={keySyncState === 'following'}
							class:armed={keySyncState !== 'off' && keySyncState !== 'following'}
							disabled={pending || (!keySyncAvailable && !deck.key_sync_enabled)}
							aria-pressed={keySyncState === 'following'}
							data-performance-control="key-sync"
							data-testid={`key-sync-deck-${deckId}`}
							aria-label={`key sync deck ${deckId}`}
							data-state={keySyncState === 'following' ? 'on' : keySyncState === 'off' ? 'off' : 'armed'}
							data-key-sync-status={keySyncState}
							onclick={async () => await onKeySync()}
						>
							KEY SYNC
						</button>
					</ControlExplainer>
					<button
						class="keysync-chevron"
						data-testid={`key-ladder-open-deck-${deckId}`}
						aria-label={`key shift ladder deck ${deckId}`}
						aria-expanded={ladderAt !== null}
						title="Every key shift -6..+6 against the KEY SYNC target, with Mixed In Key matches; pick the target deck and mode"
						disabled={deck.stable_id === null}
						onclick={openLadder}
						><svg viewBox="0 0 16 16" width="0.8em" height="0.8em" aria-hidden="true"><path d="M5 3l7 5-7 5z" fill="currentColor" /></svg
						></button
					>
					{#if ladderAt !== null}
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<div class="ladder-scrim" onclick={closeLadder} onkeydown={onLadderKey}></div>
						<div
							class="key-ladder"
							role="menu"
							tabindex="-1"
							data-testid={`key-ladder-deck-${deckId}`}
							bind:this={ladderEl}
							style={`left:${ladderAt.x}px;top:${ladderAt.y}px`}
							onkeydown={onLadderKey}
						>
							<ul class="ladder-rows">
								{#if ladderRows === null}
									<li class="ladder-empty">Load a keyed track on both this deck and the target</li>
								{:else}
									{#each ladderRows as row (row.shift)}
										<li>
											<button
												role="menuitem"
												class="ladder-row"
												class:desirable={row.desirable}
												class:current={row.shift === deck.key_shift_semitones}
												class:origin={row.shift === 0}
												data-testid={`key-ladder-row-${deckId}-${row.shift}`}
												onclick={async () => await pickShift(row.shift)}
											>
												<span class="ladder-shift">{row.shift > 0 ? `+${row.shift}` : row.shift === 0 ? ' 0' : row.shift} st</span>
												<span class="ladder-key">-&gt; {shownKey(row.key) ?? row.key}</span>
												{#if row.desirable}<span class="ladder-pattern"
														><svg class="ladder-thumb" viewBox="0 0 16 16" width="1em" height="1em" role="img" aria-label="compatible key"
															><path
																d="M2 7h2.5v7H2zM5.5 7.2 8.6 2.4c.5-.8 1.8-.4 1.8.6V6h3.1c.9 0 1.6.9 1.4 1.8l-1.2 5.1c-.2.7-.8 1.1-1.5 1.1H5.5z"
																fill="currentColor"
															/></svg
														> ({row.pattern})</span
													>{/if}
											</button>
										</li>
									{/each}
								{/if}
							</ul>
							<div class="ladder-settings">
								<span class="ladder-label">target</span>
								{#each ['master', 1, 2, 3, 4] as const as target (target)}
									{#if target !== deckId}
										<button
											class="ladder-chip"
											aria-pressed={(deck.key_sync_target ?? 'master') === target}
											data-testid={`key-sync-target-${deckId}-${target}`}
											onclick={async () => await pickKeySyncSetting({ target })}
										>{target === 'master' ? 'M' : target}</button>
									{/if}
								{/each}
								<span class="ladder-label">mode</span>
								{#each KEY_SYNC_MODE_CHIPS as chip (chip.id)}
									<button
										class="ladder-chip"
										aria-pressed={deck.key_sync_mode === chip.id}
										title={chip.title}
										data-testid={`key-sync-mode-${deckId}-${chip.id}`}
										onclick={async () => await pickKeySyncSetting({ mode: chip.id })}
									>{chip.label}</button>
								{/each}
							</div>
						</div>
					{/if}
					<div class="key-badge">
						<button
							class="nudge"
							disabled={pending || deck.stable_id === null || !nudgeFits(-1)}
							data-performance-control="key-nudge-down"
							aria-label={nudgeAria(-1)}
							data-testid={`key-nudge-down-deck-${deckId}`}
							title={nudgeTitle(-1)}
							onclick={async () => await nudge(-1)}
						>
							&lt;
						</button>
						<span
							class="key-val"
							class:key-guess={keyGuessShown !== null}
							style={keyColor !== null ? `color:${keyColor}` : undefined}
							title={keyGuessShown !== null ? `Best guess ${keyGuessShown} (ambiguous key)` : (keyHover ?? undefined)}>{keyGuessShown ?? keyShown}</span
						>
						<button
							class="key-off"
							data-testid={`key-shift-reset-deck-${deckId}`}
							title={deck.key_shift_semitones === 0 ? keyOffTitle : `${keyOffTitle}. Click to reset to the original key.`}
							disabled={pending || deck.key_shift_semitones === 0}
							onclick={resetKeyShift}>{keyShiftText} st</button
						>
						<button
							class="nudge"
							disabled={pending || deck.stable_id === null || !nudgeFits(1)}
							data-performance-control="key-nudge-up"
							aria-label={nudgeAria(1)}
							data-testid={`key-nudge-up-deck-${deckId}`}
							title={nudgeTitle(1)}
							onclick={async () => await nudge(1)}
						>
							&gt;
						</button>
					</div>
				</div>
				<div class="key-steps" role="group" aria-label={`key nudge step deck ${deckId}`}>
					{#each KEY_NUDGE_STEPS as step (step.id)}
						<button
							type="button"
							class="step"
							aria-pressed={keyNudgeStep === step.id}
							title={step.title}
							data-testid={`key-nudge-step-${step.id}-deck-${deckId}`}
							onclick={() => void runPerformanceCommandFromUi({ type: 'key_nudge_step', step: step.id })}
						>
							{step.label}
						</button>
					{/each}
				</div>
			</div>

			<div class="sync-col">
				<ControlExplainer title={beatSyncTitle} bullets={beatSyncBullets} warning={gridWarning} tooltip={TOOLTIP_ISOLATED}>
					<button
						class="rb-lit-button"
						class:lit={deck.beat_sync_enabled && !gridless}
						class:grid-uneven={gridWarning !== null}
						data-grid-warning={gridWarning}
						disabled={pending || gridless}
						aria-pressed={deck.beat_sync_enabled}
						data-performance-control="beat-sync"
						data-testid={`beat-sync-deck-${deckId}`}
						aria-label={`beat sync deck ${deckId}`}
						data-state={gridless ? 'inert' : deck.beat_sync_enabled ? 'on' : 'off'}
						onclick={async () => await onBeatSync()}
					>
						BEAT SYNC
						{#if gridWarning !== null}
							<span
								class="grid-warn"
								data-testid={`beat-sync-grid-warning-deck-${deckId}`}
								aria-label={gridWarning}
								>!</span
							>
						{/if}
					</button>
				</ControlExplainer>
				<ControlExplainer title={masterTitle} bullets={masterBullets} tooltip={TOOLTIP_ISOLATED}>
					<button
						class="rb-lit-button master-btn"
						class:lit={deck.is_master}
						class:locked={deck.is_master && masterMode === 'locked'}
						disabled={pending || deck.stable_id === null}
						aria-pressed={deck.is_master}
						data-performance-control="master"
						data-testid={`master-deck-${deckId}`}
						aria-label={masterAriaLabel}
						data-state={masterDataState}
						data-master-mode={masterMode}
						onpointerdown={startMasterHold}
						onpointerup={cancelMasterHold}
						onpointerleave={cancelMasterHold}
						onpointercancel={cancelMasterHold}
						onclick={onMasterClick}
					>
						MASTER
					</button>
				</ControlExplainer>
			</div>
		</div>
	</div>
</div>

{#if tempoEditAt !== null}
	<!-- Loaded on first open, off the /performance first paint (bundle budget, PR #6235). -->
	{#await import('./TempoEditModal.svelte') then { default: TempoEditModal }}
		<TempoEditModal
			deckId={deckId}
			x={tempoEditAt.x}
			y={tempoEditAt.y}
			onclose={() => (tempoEditAt = null)}
		/>
	{:catch error}
		<span class="tempo-edit-load-error" role="alert">The tempo editor did not load: {error instanceof Error ? error.message : String(error)}</span>
	{/await}
{/if}

<style>
	/* KEY-GUESS-01: an ambiguous key's best guess reads as a guess, not a key. */
	.key-val.key-guess {
		opacity: 0.55;
		font-style: italic;
	}
	.deck-header {
		position: relative;
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%;
		min-width: 0;
		flex: 0 0 auto;
	}
	.deck-cloud-download {
		position: absolute;
		inset: 0 0 auto 0;
		height: 3px;
		background: var(--rb-surface-2, #2a2a2a);
		pointer-events: none;
	}
	.deck-cloud-fill {
		display: block;
		height: 100%;
		background: var(--rb-accent, #4cc9f0);
	}
	/* Fixed square slot with equal inset so the thumb is padded on all sides. */
	.art-slot {
		flex: 0 0 var(--rb-deck-art, 75px);
		width: var(--rb-deck-art, 75px);
		height: var(--rb-deck-art, 75px);
		box-sizing: border-box;
		padding: 4px;
		display: grid;
		place-items: stretch;
	}
	.art {
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: center;
		border: 1px solid var(--rb-border);
		background: var(--rb-panel-raised);
		box-sizing: border-box;
	}
	.art.placeholder {
		background: var(--rb-panel-raised);
	}
	.header-body {
		display: flex;
		flex-wrap: wrap;
		align-items: flex-start;
		align-content: flex-start;
		gap: 6px;
		row-gap: 4px;
		flex: 1 1 auto;
		min-width: 0;
	}
	.deck-num {
		font-size: var(--rb-fs-deck-title);
		font-weight: 700;
		color: var(--rb-text-dim);
		flex: 0 0 auto;
		line-height: 1.2;
		padding-top: 0;
		margin-top: -2px;
	}
	.art-btn {
		position: relative;
		display: block;
		padding: 0;
		border: none;
		background: transparent;
		cursor: pointer;
		width: 100%;
		height: 100%;
		min-width: 0;
		min-height: 0;
	}
	.art-btn .art {
		display: block;
	}
	.art-btn:hover .art {
		filter: brightness(0.45);
	}
	.art-eject {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 28px;
		color: #fff;
		opacity: 0;
		pointer-events: none;
		text-shadow: 0 1px 4px #000;
	}
	.art-btn:hover .art-eject {
		opacity: 1;
	}
	.meta {
		display: flex;
		flex-direction: column;
		justify-content: center;
		min-width: 0;
		flex: 1 1 64px;
		align-self: stretch;
		max-height: var(--rb-deck-art, 75px);
	}
	.meta.empty .title {
		color: var(--rb-text-dim);
		font-weight: 400;
	}
	.title {
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.artist {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.deck-rating-row {
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: var(--rb-fs-label);
	}
	/* Pin 54c59dd3f564: "color dot should be vertically center aligned with
	 * star icons and be 20% smaller." 9px -> 7.2px is the 20%.
	 *
	 * The centring is NOT what `align-items: center` above already does. That
	 * aligns LAYOUT BOXES, and those are already flush (both centred on
	 * 249.5 in a 1280x800 deck). What the maintainer can see is the INK.
	 *
	 * This correction started as a text-glyph nudge: `.rb-star` used a `☆`
	 * character on a `line-height: 1` line box, whose ink painted LOW inside
	 * that box, and 0.045em (dot down) cancelled it. `RatingStars` now draws
	 * the star as an inline `<svg width="1em" height="1em">` (67e948592,
	 * "em-sized stars"), and an inline SVG sits on the text BASELINE instead:
	 * with `--rb-font`'s descent the 10.5px glyph lands with its ink centre
	 * 0.43px ABOVE the row centre, while the dot's bordered circle paints on
	 * its own box centre. The same nudge therefore doubled the error instead
	 * of cancelling it -- measured dot-minus-stars delta went from -0.67px on
	 * the glyph to +0.93px at the RC head, over the spec's 0.35px ceiling.
	 *
	 * `top` is the measured optical correction, in em of the STAR's font size
	 * rather than the row's, written as the SAME expression theme.css gives
	 * `.rb-star` so the two cannot drift apart: the baseline offset scales
	 * with the glyph's em, so the correction must too. In this subtree it
	 * always resolves to the `--rb-fs-browser` fallback: `--rb-star-size` is a
	 * local custom property on TrackTable's `.c-rating` cell, and a deck
	 * header is never a descendant of one, so the narrowed-rating-column case
	 * cannot reach here.
	 *
	 * -0.0435em is the measured 0.93px swing minus the old +0.4725px at the
	 * 10.5px star em (mono-dev / light skins); residual 0.00px on the outline
	 * glyph and within 0.35px at every filled rating. Measured, not assumed --
	 * performance-deck-color-dot.spec.ts reads the composited pixels back and
	 * fails if this is reverted. */
	.color-dot {
		display: inline-block;
		width: 7.2px;
		height: 7.2px;
		border-radius: 50%;
		border: 1px solid var(--rb-text-dim);
		background: transparent;
		font-size: var(--rb-star-size, var(--rb-fs-browser));
		position: relative;
		top: -0.0435em;
	}
	.readout {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		flex: 0 1 auto;
		min-width: 0;
	}
	.readout .bpm {
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.readout-key-line,
	.readout-tempo-line {
		font-size: 0.75em;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.readout .from {
		color: var(--rb-text-dim);
	}
	.reset-to-original {
		background: transparent;
		border: none;
		color: inherit;
		font: inherit;
		text-align: left;
		padding: 2px 4px;
		cursor: pointer;
	}
	.clocks {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		flex: 0 1 auto;
		min-width: 0;
		font-variant-numeric: tabular-nums;
	}
	.clocks .remain {
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
	}
	.clocks .elapsed {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
	}
	.keysync {
		flex: 0 0 auto;
		width: 100%;
	}
	.key-stack {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 2px;
		flex: 0 0 auto;
	}
	/* Same width as the button, so the key value and shift land under the
	   KEY SYNC label instead of centering in the wider step row. */
	.key-pair {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 2px;
		flex: 0 0 auto;
	}
	.key-steps {
		display: flex;
		gap: 2px;
	}
	.key-steps .step {
		flex: 1 1 auto;
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font: inherit;
		font-size: 9px;
		line-height: 1.3;
		padding: 0 3px;
		cursor: pointer;
	}
	.key-steps .step[aria-pressed='true'] {
		color: var(--rb-text);
		border-color: var(--rb-text-dim);
		background: var(--rb-panel-raised);
	}
	.keysync.armed {
		outline: 1px dashed var(--rb-text-dim);
		outline-offset: -2px;
	}
	.enable-master-tempo {
		border: 1px solid var(--rb-red);
		border-radius: 2px;
		background: color-mix(in srgb, var(--rb-red) 15%, var(--rb-panel-raised));
		color: var(--rb-red);
		font: inherit;
		font-weight: 650;
		padding: 3px 5px;
		cursor: pointer;
	}
	.enable-master-tempo:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.chrome {
		display: flex;
		flex-wrap: nowrap;
		align-items: flex-start;
		gap: 6px;
		flex: 0 0 auto;
		margin-left: auto;
	}
	.key-badge {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 2px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		padding: 1px 3px;
		flex: 0 0 auto;
	}
	.key-badge .nudge {
		background: none;
		border: none;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		padding: 0 2px;
		cursor: default;
	}
	.key-badge .key-val {
		font-size: var(--rb-fs-label);
		font-weight: 600;
	}
	.key-badge .key-off {
		font: inherit;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
	}
	.key-badge .key-off:disabled {
		cursor: default;
	}
	.key-pair {
		position: relative;
	}
	.keysync-chevron {
		position: absolute;
		top: 0;
		right: -12px;
		width: 11px;
		height: 18px;
		padding: 0;
		background: none;
		border: none;
		color: var(--rb-text-dim);
		font-size: 10px;
		cursor: pointer;
		display: grid;
		place-items: center;
	}
	.ladder-scrim {
		position: fixed;
		inset: 0;
		z-index: 60;
	}
	/* Fixed, centred on the chevron: +6 above, 0 level with KEY SYNC, -6 below. */
	.key-ladder {
		position: fixed;
		z-index: 61;
		transform: translateY(-50%);
		display: flex;
		gap: 4px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		padding: 3px;
		box-shadow: 0 4px 14px rgb(0 0 0 / 0.5);
	}
	.ladder-rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
	}
	.ladder-row {
		display: flex;
		gap: 6px;
		width: 100%;
		background: none;
		border: none;
		border-radius: 2px;
		color: var(--rb-text-dim);
		font: inherit;
		font-size: var(--rb-fs-label);
		padding: 1px 4px;
		text-align: left;
		white-space: nowrap;
		cursor: pointer;
	}
	.ladder-row:hover {
		background: var(--rb-panel);
	}
	.ladder-row.desirable {
		color: var(--rb-text);
	}
	.ladder-row.origin {
		border-top: 1px solid var(--rb-border);
		border-bottom: 1px solid var(--rb-border);
	}
	.ladder-row.current {
		outline: 1px solid var(--rb-text-dim);
	}
	.ladder-thumb {
		vertical-align: -0.125em;
	}
	.ladder-shift {
		min-width: 3.4em;
		font-variant-numeric: tabular-nums;
	}
	.ladder-empty {
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		padding: 2px 4px;
	}
	.ladder-settings {
		display: flex;
		flex-direction: column;
		gap: 2px;
		align-self: center;
	}
	.ladder-label {
		font-size: 9px;
		color: var(--rb-text-dim);
	}
	.ladder-chip {
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font: inherit;
		font-size: 9px;
		padding: 0 3px;
		cursor: pointer;
	}
	.ladder-chip[aria-pressed='true'] {
		color: var(--rb-text);
		border-color: var(--rb-text-dim);
	}
	.sync-col {
		display: flex;
		flex-direction: column;
		gap: 2px;
		flex: 0 0 auto;
	}
	/* Uneven-grid marker: sits in the button's corner so the label, and the
	   header's layout, do not move when a track with a rough grid loads. */
	.sync-col .grid-uneven {
		position: relative;
		border-color: var(--rb-orange, #e8952a);
	}
	.grid-warn {
		position: absolute;
		top: -5px;
		right: -3px;
		min-width: 11px;
		padding: 0 2px;
		border-radius: 6px;
		background: var(--rb-orange, #e8952a);
		color: #111;
		font-size: 9px;
		font-weight: 700;
		line-height: 11px;
		text-align: center;
	}
	.master-btn.lit {
		color: var(--rb-master-ink);
		background: var(--rb-master);
		box-shadow: 0 0 6px color-mix(in srgb, var(--rb-master) 45%, transparent);
		border-color: color-mix(in srgb, var(--rb-master) 85%, var(--rb-text));
	}
	.master-btn.lit.locked {
		box-shadow:
			inset 0 0 0 1px color-mix(in srgb, var(--rb-master-ink) 55%, transparent),
			0 0 6px color-mix(in srgb, var(--rb-master) 45%, transparent);
	}
	.master-btn.lit.locked::after {
		content: 'LOCK';
		position: absolute;
		right: 2px;
		bottom: 1px;
		font-size: 7px;
		line-height: 1;
		letter-spacing: 0.04em;
		opacity: 0.85;
		pointer-events: none;
	}
	.master-btn {
		position: relative;
	}
	/* DECKUX-8360: mirrored (right-side) deck. Art on the outer edge, then the
	   deck number and title; BEAT SYNC / MASTER, KEY SYNC, BPM and clocks on
	   the inner edge beside the screen centre. Only the box order flips: every
	   label still reads left to right. */
	.deck-header.mirrored,
	.deck-header.mirrored .header-body,
	.deck-header.mirrored .chrome {
		flex-direction: row-reverse;
	}
	.deck-header.mirrored .chrome {
		margin-left: 0;
		margin-right: auto;
	}
	.deck-header.mirrored .meta {
		align-items: flex-end;
		text-align: right;
	}
	.deck-header.mirrored .readout {
		align-items: flex-end;
	}
	.deck-header.mirrored .clocks {
		align-items: flex-start;
	}
</style>
