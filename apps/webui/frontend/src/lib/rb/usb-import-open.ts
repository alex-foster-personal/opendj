/**
 * Open the USB import analysis plan. Loaded on the import click, not at
 * first paint: it reads the stick library and whatever deck is playing.
 */
import { DECK_IDS } from '$lib/player/constants';
import { keyAtPlayhead } from '$lib/player/key-playhead';
import { deckEffectiveBpm, getDeckState } from '$lib/player/state.svelte';
import { revalidateUsbLibrary, usbVolumeUuid } from './usb-library.svelte';
import { rankUsbImport, type UsbImportContext } from './usb-import-rank';
import {
	beginUsbImportPlan,
	failUsbImportPlan,
	publishUsbImportPlan,
	usbImportPlanCurrent
} from './usb-import-view.svelte';
import type { UsbVolumeKnown } from './usb-tracker.svelte';

type DeckKeyFields = Pick<
	ReturnType<typeof getDeckState>,
	'anlz' | 'position_ms' | 'key_shift_semitones' | 'key'
>;

/** The key the deck sounds in now: its key segment at the playhead, shifted by
 * KEY SYNC or a manual shift, the same key DeckHeader shows. null when unknown. */
export function audibleDeckKey(st: DeckKeyFields): string | null {
	const shown = keyAtPlayhead(st.anlz, st.position_ms, st.key_shift_semitones, st.key).display;
	return shown === null || shown === '--' ? null : shown;
}

export function readPerformanceContext(): UsbImportContext {
	let playing: UsbImportContext | null = null;
	let loaded: UsbImportContext | null = null;
	for (const deck of DECK_IDS) {
		const st = getDeckState(deck);
		const ctx: UsbImportContext = {
			playing: st.playing,
			bpm: deckEffectiveBpm(deck),
			key: audibleDeckKey(st)
		};
		if (st.playing && st.is_master) return ctx;
		if (st.playing && playing === null) playing = ctx;
		if (!st.playing && loaded === null && st.stable_id !== null) {
			loaded = { playing: false, bpm: ctx.bpm, key: ctx.key };
		}
	}
	return playing ?? loaded ?? { playing: false, bpm: null, key: null };
}

/** Why this stick gets no ranked plan, or null when it can have one. Only a
 * rekordbox (RBX) export has a track list the daemon can read before the
 * import lands (GET /usb/{id}/library reads export.pdb); djay and plain music
 * sticks are still imported and analyzed, just not ranked. */
export function usbPlanRefusal(vol: Pick<UsbVolumeKnown, 'id' | 'kind'>): string | null {
	if (vol.kind !== 'rekordbox') {
		return 'This stick has no rekordbox export, so its tracks cannot be ranked here. The stick is still imported and analyzed.';
	}
	if (!vol.id.startsWith('vol:')) {
		return 'This volume has no id, so its tracks cannot be ranked. The stick is still queued.';
	}
	return null;
}

/** Read the stick and publish a performance-aware analysis order. */
export async function openUsbImportPlan(vol: UsbVolumeKnown): Promise<void> {
	const mine = beginUsbImportPlan(vol.id, vol.name);
	const refusal = usbPlanRefusal(vol);
	if (refusal !== null) {
		failUsbImportPlan(mine, refusal);
		return;
	}
	try {
		// Open is an explicit read: a cached export would rank a stale track
		// list after export.pdb was rewritten in place (Sol P1, PR #6223).
		const library = await revalidateUsbLibrary(usbVolumeUuid(vol.id));
		if (!usbImportPlanCurrent(mine)) return;
		const plan = rankUsbImport(library.tracks, readPerformanceContext());
		publishUsbImportPlan(mine, plan);
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err);
		failUsbImportPlan(mine, message);
	}
}
