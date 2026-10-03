/**
 * Which stem tiers the right-click menu offers (INSTALL-32).
 *
 * Product rule: hide rather than show disabled. A tier this engine refuses to
 * spawn -- a Modal tier in the installed app, which ships no uv and no modal --
 * is left out of the menu entirely. The signal is the engine's own
 * `runnable_here` from GET /stems/tiers, computed by the same predicate that
 * makes POST /stems/generate refuse with 503 (the backstop), so the menu never
 * guesses at "packaged" from anything in the browser.
 *
 * NOT_APPLICABLE rungs are a different thing (measured and deliberately not
 * offered) and keep their inert "(n/a)" item; only `runnable_here` hides.
 */
import type { StemTier } from './api-rb';

export function menuStemTiers(tiers: readonly StemTier[]): StemTier[] {
	return tiers.filter((tier) => tier.runnable_here);
}
