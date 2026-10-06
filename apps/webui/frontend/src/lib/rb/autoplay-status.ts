/**
 * AGENT-20: AutoPlay's own state, as the UI-mirror publishes it.
 *
 * Before this, an agent reading GET /api/v1/state/ui-mirror could see the
 * decks and a PLAY-08 stall, but not whether AutoPlay was switched on at all,
 * nor whether it would actually hand off from the playing track. Bug #2c (Tue 6
 * Oct 2026, silver preview soak): the soak agent could not tell "AutoPlay off"
 * from "AutoPlay on with nothing to arm off".
 *
 * Three fields, one invariant: `autoplay_disarm_reason` is null exactly when
 * `autoplay_armed` is true, and armed implies enabled. The server enforces the
 * same invariant on ingest (apps/webui/server/routes/state.py), so a publisher
 * that drifts from this function is refused rather than silently believed.
 *
 * Pure: the live reader is `readAutoPlayMirrorStatus` in auto-play.svelte.ts.
 */
import type { AutoPlayStallReason } from '$lib/rb/autoplay-stall';

/** Why AutoPlay will not hand off right now. Stall reasons pass through verbatim. */
export type AutoPlayDisarmReason =
	| 'disabled'
	| 'not-installed'
	| 'no-deck-playing'
	| 'no-playing-master'
	| AutoPlayStallReason;

export interface AutoPlayMirrorStatus {
	autoplay_enabled: boolean;
	autoplay_armed: boolean;
	autoplay_disarm_reason: AutoPlayDisarmReason | null;
}

export interface AutoPlayStatusInput {
	/** The operator's toggle, `uiPrefs.auto_play_enabled`. */
	enabled: boolean;
	/** The controller is mounted on this page (installAutoPlay ran, not torn down). */
	installed: boolean;
	/** The active PLAY-08 stall, or null. A stalled AutoPlay will not hand off. */
	stall_reason: AutoPlayStallReason | null;
	/** A deck the controller would arm off: a playing master, or a silence-recovery source. */
	has_source: boolean;
	/** A handoff is in flight or a master promotion is settling. */
	handoff_pending: boolean;
	any_playing: boolean;
}

/** First matching reason wins; the order is the order an operator would fix them in. */
export function classifyAutoPlayStatus(input: AutoPlayStatusInput): AutoPlayMirrorStatus {
	const disarmed = (reason: AutoPlayDisarmReason): AutoPlayMirrorStatus => ({
		autoplay_enabled: input.enabled,
		autoplay_armed: false,
		autoplay_disarm_reason: reason
	});
	if (!input.enabled) return disarmed('disabled');
	if (!input.installed) return disarmed('not-installed');
	if (input.stall_reason !== null) return disarmed(input.stall_reason);
	if (input.has_source || input.handoff_pending) {
		return { autoplay_enabled: true, autoplay_armed: true, autoplay_disarm_reason: null };
	}
	return disarmed(input.any_playing ? 'no-playing-master' : 'no-deck-playing');
}
