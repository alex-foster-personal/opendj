/**
 * ONE-WAY IMPORT MODE, read once, applied everywhere in the UI.
 *
 * The daemon refuses every write toward real rekordbox data unless an operator
 * turns the gate on (apps/shared/rekordbox_writeback.py). This module is the
 * UI half: it probes `GET /api/v1/rekordbox/writeback-gate` and hands every
 * sync-triggering control ONE refusal string, so a disabled button and the
 * server refusing the same call can never disagree about the reason.
 *
 * FAIL CLOSED. The refusal starts non-null and only a successful probe that
 * answers `enabled: true` clears it. A daemon that is down, slow, or older
 * than this route therefore leaves every rekordbox write control inert --
 * which is the safe direction, and the same rule capabilities.svelte.ts uses
 * for an unidentified daemon.
 *
 * The tooltip is deliberately NOT the PARITY-TODO stub wording that
 * inert-controls.test.mjs polices. That one means "not built".
 * These features ARE built and work; they are switched off on purpose. Two
 * different facts, two different sentences.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 rekordboxWritebackRefusal() is non-null before any probe resolves.
 *     [if] a control renders live while the gate is unknown [then ⛔️] broken
 *   ✔︎ 🎯 probe(): one GET, memoized on success, retried after failure.
 *     [if] two probe() calls issue two requests after a success [then ⛔️] broken
 *   ✔︎ 🎯 a probe answering enabled:false keeps the refusal, and reports the
 *     server's own ui_title rather than a second hardcoded copy.
 *     [if] the UI wording drifts from UI_REFUSAL_TITLE [then ⛔️] broken
 */

import { api, unwrap } from '$lib/api/client';

/** Fallback wording, used only before the daemon has told us its own. Kept
 * byte-identical to UI_REFUSAL_TITLE in apps/shared/rekordbox_writeback.py. */
export const WRITEBACK_DISABLED_TITLE = 'sync to rekordbox disabled - one-way import only';

/** Shown while the daemon has not answered. Still a refusal: unknown is off. */
const UNPROBED = WRITEBACK_DISABLED_TITLE;

class RekordboxWritebackGate {
	/** false until a probe proves otherwise. Never optimistic. */
	enabled = $state(false);
	/** The daemon's own tooltip once it has answered. */
	title = $state(WRITEBACK_DISABLED_TITLE);
	/** Last probe failure, verbatim. null while healthy or before the first try. */
	error = $state<string | null>(null);

	/** In flight or settled-successful probe. Cleared on failure so the next
	 * caller retries instead of inheriting "we never found out". */
	#probe: Promise<boolean> | null = null;

	async probe(): Promise<boolean> {
		if (this.#probe !== null) return this.#probe;
		const running = this.#run();
		this.#probe = running;
		return running;
	}

	async #run(): Promise<boolean> {
		try {
			const body = await unwrap(api.GET('/api/v1/rekordbox/writeback-gate'));
			this.enabled = body.enabled === true;
			this.title = body.ui_title || WRITEBACK_DISABLED_TITLE;
			this.error = null;
		} catch (exc) {
			// Not memoized: the daemon may simply not be up yet. Stays disabled.
			this.#probe = null;
			this.enabled = false;
			this.title = WRITEBACK_DISABLED_TITLE;
			this.error = exc instanceof Error ? exc.message : String(exc);
		}
		return this.enabled;
	}

	/** Drop the memo, for tests. App code probes once and lives with it. */
	_resetForTests(): void {
		this.#probe = null;
		this.enabled = false;
		this.title = WRITEBACK_DISABLED_TITLE;
		this.error = null;
	}
}

/** The one gate store. */
export const rekordboxWriteback = new RekordboxWritebackGate();

/**
 * Why a rekordbox-write control is inert, or null when the daemon allows it.
 *
 * Same shape as jobsRefusal / progressRefusal: ONE function that both gates the
 * request and supplies the tooltip, so a disabled control can never disagree
 * with the reason it is disabled.
 */
export function rekordboxWritebackRefusal(): string | null {
	if (rekordboxWriteback.enabled) return null;
	return rekordboxWriteback.title || UNPROBED;
}
