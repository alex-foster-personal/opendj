/**
 * Pin the rescue auto-restore's input for a spec that does not test it.
 *
 * Shared by zz-autoplay-playlist-switch.spec.ts (decks),
 * ui-contrast-audit.spec.ts (deck layout), quickload-dblclick-guard.spec.ts
 * (#6242: deck 2 held another spec's track) and setup-entry-points.spec.ts
 * (#6244: online-artwork 404s for a restored deck's track). The snapshot names no deck and
 * carries no deck_layout, so a restore that reads it loads nothing and leaves
 * the layout alone.
 */
import { expect, type APIRequestContext } from '@playwright/test';

/**
 * Every /performance load auto-restores the newest Gig rescue snapshot on the
 * engine (RESCUE-02/07): within 10 minutes it reloads AND resumes the decks it
 * names. The ring is server-side, so it outlives the browser context. The
 * fixture runs in Gig posture, so any earlier spec that played audio, or this
 * file's own previous case, leaves a playing snapshot behind, usually of the
 * same first-listed tracks this spec uses. Restored decks then collide with
 * this spec's fixed deck plan: deck 1 refuses the source load ("must be fully
 * stopped before replacement"), or deck 2 is taken by the source so AutoPlay
 * correctly puts the replacement on deck 3. Both were seen in CI on Tue 6 Oct
 * 2026, in about 1 run in 10, whenever a playing snapshot was still inside the
 * window. In those runs a freshly loaded page had deck 1 playing within 3.3 s,
 * before the spec had loaded anything.
 *
 * Rescue restore is not under test here, so pin its input: append a Gig
 * snapshot with every deck empty before each page load. It becomes the newest
 * entry, so restore has nothing to load or resume, and the decks this spec
 * asserts on start empty for the reason the spec states, not by timing.
 */
export async function appendEmptyRescueSnapshot(request: APIRequestContext): Promise<void> {
	const deck = (deck_id: number) => ({
		deck_id,
		stable_id: null,
		source_path: null,
		playing: false,
		position_ms: 0,
		beat_stamp: { kind: 'sample', position_ms: 0 },
		pitch: 1,
		pitch_range: 8,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		quantize_enabled: true,
		beat_sync_enabled: true,
		sync_mode: 'bar',
		is_master: false,
		cue_ms: null,
		loop: null,
		hot_cue_armed: null,
		stems: {
			vocal: { muted: false, solo: false, gain: 0.5 },
			instrumental: { muted: false, solo: false, gain: 0.5 },
			drums: { muted: false, solo: false, gain: 0.5 }
		},
		mixer_channel: {
			trim: 0.5,
			eq_high: 0.5,
			eq_mid: 0.5,
			eq_low: 0.5,
			filter: 0.5,
			fader: 1,
			assign: 'THRU',
			cue_enabled: false
		}
	});
	const capturedAt = Date.now();
	const response = await request.post('/api/v1/performance/rescue-snapshots', {
		data: {
			schema: 1,
			captured_at_ms: capturedAt,
			reason: 'periodic',
			app_posture: 'gig',
			master_deck: null,
			playlist_id: null,
			decks: { 1: deck(1), 2: deck(2), 3: deck(3), 4: deck(4) },
			mixer: {
				crossfader: 0.5,
				master: 0.1,
				headphones: {
					mix: 0.5,
					level: 0.5,
					output_mode: 'practice',
					selected_master_output_device_id: null,
					selected_output_device_id: null
				}
			}
		}
	});
	if (!response.ok()) {
		throw new Error(`append empty rescue snapshot failed (${response.status()}): ${await response.text()}`);
	}
	// Read back what the page's auto-restore will read: the newest ring entry
	// must name no deck, or the next page load restores one into this case.
	const listed = await request.get('/api/v1/rescue/snapshots');
	if (!listed.ok()) {
		throw new Error(`list rescue snapshots failed (${listed.status()}): ${await listed.text()}`);
	}
	const { snapshots } = (await listed.json()) as {
		snapshots: Array<{ captured_at_ms: number; deck_count_loaded: number }>;
	};
	// Identity, not shape: the newest entry must be THIS snapshot (no decks, no
	// deck_layout), so a newer one from a writer still running cannot pass.
	expect(snapshots[0]?.captured_at_ms, 'the newest rescue snapshot is not the one just pinned').toBe(capturedAt);
	expect(snapshots[0]?.deck_count_loaded, 'the newest rescue snapshot still names a deck').toBe(0);
}
