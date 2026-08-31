/**
 * WHAT THIS ACCOUNT MAY DO, decided by the daemon, read once.
 *
 * A SIBLING of `capabilities.svelte.ts`, not a replacement for it, because
 * they answer two different questions and a control can be dead for either
 * reason:
 *
 *   capabilities -- "does the daemon behind this page OFFER this?"  A legacy
 *     boot has no jobs API; the engine has no progress ledger. Nothing to do
 *     with the user.
 *   entitlements -- "is this ACCOUNT allowed to?"  Billing-owned, per account,
 *     and today the answer is yes to everything.
 *
 * THE THIRD STATE (ENT-03). There are now three reasons a control is inert and
 * each gets its own sentence, because telling a user "not implemented" when
 * the truth is "not in your plan" is a lie about their own software:
 *
 *   1. `INERT_TITLE`         'not implemented - see PARITY-TODO'  -- not built
 *   2. the capability refusals in capabilities.svelte.ts          -- this
 *                                                                    daemon
 *                                                                    does not
 *                                                                    offer it
 *   3. `PLAN_REFUSAL_TITLE`, below                                -- built,
 *                                                                    offered,
 *                                                                    and not
 *                                                                    on your
 *                                                                    plan
 *
 * `tests/unit/plan-refusal.test.mjs` pins all three apart, and pins this
 * module's copy of the sentence against the server's
 * (`apps/entitlements/resolver.py:UI_REFUSAL_TITLE`) so the two halves of the
 * wire cannot drift.
 *
 * WHY THE WORDING IS DUPLICATED AT ALL. The server sends `refusal.ui_title` on
 * every response and `planRefusal` returns THAT once loaded, so the disabled
 * control and the server refusing the request always agree (ENT-02). The
 * constant here is what a control shows in the tick BEFORE the first response
 * lands, and the test is what stops it becoming a second, drifting truth.
 *
 * NOTHING IS GATED TODAY. `features` comes back empty because openDJ has no
 * paid features, so `planRefusal` returns null for everything. That is the
 * inert-and-honest state ENT-04 asks for, not a stub that fakes a plan: the
 * seam is real, the answer is genuinely yes.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 planRefusal returns null for a feature the daemon reports as
 *     entitled, and for one it has never heard of.
 *     [if] an uncatalogued feature id reads as refused [then ⛔️] broken
 *   ✔︎ 🎯 planRefusal returns the SERVER's ui_title once loaded, never a
 *     locally composed sentence.
 *     [if] the tooltip and the 4xx body disagree [then ⛔️] broken
 *   ✔︎ 🎯 an unresolved load leaves every feature un-refused rather than
 *     refused, so a slow daemon never reads as "you may not".
 *     [if] a control is disabled while entitlements are still loading
 *     [then ⛔️] broken
 */

import { api, unwrap } from './client';
import type { components } from '../api-types';

type EntitlementsOut = components['schemas']['EntitlementsOut'];
type FeatureEntitlementOut = components['schemas']['FeatureEntitlementOut'];
type PlanOut = components['schemas']['PlanOut'];

/**
 * The tooltip a plan-gated control carries. Kept identical to
 * `apps/entitlements/resolver.py:UI_REFUSAL_TITLE`, and deliberately unlike
 * both the PARITY-TODO wording and the capability refusals.
 */
export const PLAN_REFUSAL_TITLE =
	'not included in your plan - see your account for what is included';

function _message(exc: unknown): string {
	return exc instanceof Error ? exc.message : String(exc);
}

class EntitlementStore {
	/** Null until the first successful load. */
	plan = $state<PlanOut | null>(null);
	/** Every feature the daemon can gate. Empty while nothing is gated. */
	features = $state<FeatureEntitlementOut[]>([]);
	/** The server's refusal wording, once known. */
	refusalTitle = $state<string>(PLAN_REFUSAL_TITLE);
	/** Last load failure, verbatim. Null while healthy or before the first try. */
	error = $state<string | null>(null);
	/** True once a load has succeeded, so callers can tell "yes" from "not yet". */
	loaded = $state(false);

	/** In flight or settled-successful load. Cleared on failure so the next
	 * caller retries rather than inheriting "we never found out". */
	#load: Promise<void> | null = null;

	/** Resolve the entitlement set. Memoized on success, like the health probe. */
	async load(): Promise<void> {
		if (this.#load !== null) return this.#load;
		const running = this.#run();
		this.#load = running;
		return running;
	}

	async #run(): Promise<void> {
		try {
			const body = (await unwrap(api.GET('/api/v1/entitlements'))) as EntitlementsOut;
			this.plan = body.plan;
			this.features = body.features;
			this.refusalTitle = body.refusal.ui_title;
			this.error = null;
			this.loaded = true;
		} catch (exc) {
			this.#load = null;
			this.error = _message(exc);
			console.error('[entitlements] load failed; nothing is treated as refused', exc);
		}
	}

	/** The daemon's answer for one feature, or null if it has never heard of it. */
	feature(featureId: string): FeatureEntitlementOut | null {
		return this.features.find((row) => row.feature_id === featureId) ?? null;
	}

	/** Drop the memo, for tests. App code loads once and lives with it. */
	_resetForTests(): void {
		this.#load = null;
		this.plan = null;
		this.features = [];
		this.refusalTitle = PLAN_REFUSAL_TITLE;
		this.error = null;
		this.loaded = false;
	}
}

/** The one entitlement store. */
export const entitlements = new EntitlementStore();

/**
 * Why a feature is not available on this account, or null when it is.
 *
 * The same shape as `jobsRefusal()` / `progressRefusal()`: one function that
 * both gates the request and supplies the tooltip, so a disabled control can
 * never disagree with the reason it is disabled.
 *
 * A feature the daemon does not list is NOT refused. While no payment provider
 * is configured everything is entitled (ENT-04), and treating an unlisted id
 * as a denial would switch a working feature off the moment somebody added a
 * call site before its catalog entry.
 *
 * IT FAILS OPEN, DELIBERATELY. An unresolved or failed load returns null, so a
 * slow or unreachable daemon never reads as "you may not". That is safe here
 * in a way it would not be in most apps, because this function is not the
 * enforcement point and is never meant to be: the governing rule in
 * `specs/saas-spec.md` is that a paid feature must be one the SERVER refuses
 * to do, so the worst a wrong `null` can do is let a control stay live until
 * the server answers. A client-side check that failed CLOSED would be
 * pretending to be the gate, which is the DRM this project rejected outright.
 */
export function planRefusal(featureId: string): string | null {
	if (!featureId.trim()) {
		throw new Error('planRefusal needs a feature id; an empty one has no honest answer');
	}
	if (!entitlements.loaded) return null;
	const row = entitlements.feature(featureId);
	if (row === null || row.entitled) return null;
	return entitlements.refusalTitle;
}
