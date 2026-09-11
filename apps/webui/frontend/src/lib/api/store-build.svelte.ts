/**
 * WHAT THIS BUILD CANNOT DO, decided by the daemon, read once.
 *
 * The THIRD sibling of `capabilities.svelte.ts` and `entitlements.svelte.ts`.
 * All three answer "why is this control dead", and they answer differently,
 * which is the entire point:
 *
 *   capabilities  -- "does the daemon behind this page OFFER this?"
 *   entitlements  -- "is this ACCOUNT allowed to?"
 *   store build   -- "does this BUILD have it at all?"  Apple makes the App
 *                    Sandbox mandatory on the Mac App Store, and a sandboxed
 *                    process may not list /Volumes or read another app's
 *                    library. The store build ships those features OFF.
 *
 * THE FOURTH STATE (SAND-01). Four reasons a control is inert, four sentences,
 * because telling a user "not implemented" when the truth is "Apple's sandbox
 * forbids it" is a lie about their own software:
 *
 *   1. `INERT_TITLE`               'not implemented - see PARITY-TODO'
 *   2. the capabilities.svelte.ts refusals   this daemon does not offer it
 *   3. `PLAN_REFUSAL_TITLE`        built, offered, not on your plan
 *   4. `STORE_BUILD_REFUSAL_TITLE`, below    built, offered, entitled, and
 *                                            absent from THIS build
 *
 * `tests/unit/store-build-refusal.test.mjs` pins all four apart, reading the
 * fourth off `apps/shared/sandbox.py:STORE_BUILD_REFUSAL_TITLE`.
 *
 * THERE IS NO COPY OF THE SENTENCE HERE, on purpose, and that is a difference
 * from `entitlements.svelte.ts` rather than an oversight. That module keeps
 * `PLAN_REFUSAL_TITLE` because a plan-gated control has to render something in
 * the tick before the first response lands. This one has nothing to render in
 * that tick: an unresolved load refuses nothing at all, so a fallback sentence
 * would never be shown. Spelling it here anyway would manufacture a second
 * truth that can drift, purely so a test could check it had not.
 *
 * IT MUST NOT POINT AT A DOWNLOAD. App Store Review Guideline 3.2.2(vi) bars
 * requiring a user to fetch something else to access functionality, and a
 * tooltip reading "use the direct download instead" is that pattern shipped in
 * a string. Shipping a store build without the feature is fine; advertising
 * the way around it from inside that build is a rejection risk. The test
 * asserts the sentence names no such escape hatch.
 *
 * WHY AN UNDECLARED FLAG THROWS, unlike `planRefusal`. An entitlement id the
 * daemon has never heard of is genuinely un-gated (ENT-04), so null is the
 * true answer. A flag id nobody declared is a TYPO, and answering "not
 * refused" would bury it forever - exactly why the server's `FlagStore.enabled`
 * raises rather than defaulting to off. The two halves of the wire keep the
 * same rule.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 storeBuildRefusal returns the SERVER's ui_title for a flag the store
 *     profile turned off, and this module composes no sentence of its own.
 *     [if] the tooltip and the server's refusal disagree [then ⛔️] broken
 *   ✔︎ 🎯 it returns null on a full build, for every flag, including off ones.
 *     [if] a flag off in a dev build reads as an App Store refusal [then ⛔️] broken
 *   ✔︎ 🎯 an unresolved load leaves every flag un-refused, so a slow daemon
 *     never reads as "this build does not have it".
 *     [if] a control refuses while flags are still loading [then ⛔️] broken
 *   ✔︎ 🎯 an undeclared flag id throws once loaded.
 *     [if] a typo'd flag id reads as available [then ⛔️] broken
 */

import { api, unwrap } from './client';
import type { components } from '../api-types';

type FlagsOut = components['schemas']['FlagsOut'];
type FlagOut = components['schemas']['FlagOut'];

/** The profile name meaning "ship everything". Mirrors profiles.DEFAULT_PROFILE.
 * Module-private: it is the pre-load placeholder, not a value a caller should
 * branch on. Ask `storeBuildRefusal`, which reads the server's own verdict. */
const FULL_PROFILE = 'full';

function _message(exc: unknown): string {
	return exc instanceof Error ? exc.message : String(exc);
}

class BuildFlagStore {
	/** Every declared flag, with the server's refusal attached where it applies. */
	flags = $state<FlagOut[]>([]);
	/** The named build profile the daemon resolved. `full` until known. */
	profile = $state<string>(FULL_PROFILE);
	/**
	 * Whether the DAEMON process is inside an App Sandbox container. Reported
	 * beside the profile rather than folded into it because the two can
	 * disagree, and SAND-04 turns on noticing when they do: a mis-packaged
	 * bundle is sandboxed on the full profile, and a developer can select the
	 * store profile without being sandboxed at all.
	 */
	sandboxed = $state(false);
	/** Last load failure, verbatim. Null while healthy or before the first try. */
	error = $state<string | null>(null);
	/** True once a load has succeeded, so callers can tell "no" from "not yet". */
	loaded = $state(false);

	#load: Promise<void> | null = null;

	/** Resolve the flag set. Memoized on success, like the health probe. */
	async load(): Promise<void> {
		if (this.#load !== null) return this.#load;
		const running = this.#run();
		this.#load = running;
		return running;
	}

	async #run(): Promise<void> {
		try {
			const body = (await unwrap(api.GET('/api/v1/flags'))) as FlagsOut;
			this.flags = body.flags;
			this.profile = body.build_profile;
			this.sandboxed = body.sandboxed;
			this.error = null;
			this.loaded = true;
		} catch (exc) {
			// Not memoized: the daemon may simply not be up yet.
			this.#load = null;
			this.error = _message(exc);
			console.error('[store-build] flag load failed; nothing is treated as absent', exc);
		}
	}

	/** One declared flag, or null when the daemon does not declare it. */
	flag(flagId: string): FlagOut | null {
		return this.flags.find((row) => row.flag_id === flagId) ?? null;
	}

	/** Drop the memo, for tests. App code loads once and lives with it. */
	_resetForTests(): void {
		this.#load = null;
		this.flags = [];
		this.profile = FULL_PROFILE;
		this.sandboxed = false;
		this.error = null;
		this.loaded = false;
	}
}

/** The one build-flag store. */
export const buildFlags = new BuildFlagStore();

/**
 * Why this build does not have a feature, or null when it does.
 *
 * The same shape as `planRefusal()` / `jobsRefusal()`: one function that both
 * gates the surface and supplies the tooltip, so a disabled control can never
 * disagree with the reason it is disabled.
 *
 * The sentence comes from the SERVER's `refusal.ui_title`, never composed
 * here, so the panel and any 503 body say the same words. The server attaches
 * it only when the App Store profile is the one that turned the flag off - so
 * a flag switched off in a developer build correctly returns null rather than
 * blaming Apple for a local override.
 *
 * IT FAILS OPEN while unresolved, for the same reason `planRefusal` does: a
 * slow daemon must not make a working control read as missing.
 */
export function storeBuildRefusal(flagId: string): string | null {
	if (!flagId.trim()) {
		throw new Error('storeBuildRefusal needs a flag id; an empty one has no honest answer');
	}
	if (!buildFlags.loaded) return null;
	const row = buildFlags.flag(flagId);
	if (row === null) {
		throw new Error(
			`storeBuildRefusal: no flag ${flagId} is declared by this daemon. ` +
				'A flag id that answers "not refused" because nobody declared it is a ' +
				'typo nothing will ever surface; declare it in ' +
				'apps/feature_flags/store.FLAGS or fix the call site.'
		);
	}
	return row.refusal?.ui_title ?? null;
}
