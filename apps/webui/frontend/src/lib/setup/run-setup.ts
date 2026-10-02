/**
 * THE way back into the setup wizard, for every surface that is not the
 * first-run gate.
 *
 * ORIGIN. The first-run overlay is a one-shot: once setup has run, or the
 * tester pressed "Skip for now", the engine records the dismissal in the data
 * dir and the overlay never comes back. That was the whole story, so there was
 * NO way back into setup from inside a running app. This module is the way
 * back, and it is deliberately one module rather than one copy per entry
 * point: the Cmd+, settings overlay, the /admin Setup tab and the /settings
 * page all call `runSetup`, so three buttons cannot drift into three
 * different behaviours.
 *
 * WHY RE-ARM RATHER THAN JUST NAVIGATE. The dismissal lives engine-side (in
 * the data directory), not in this tab. Navigating to /setup without clearing
 * it leaves the app in a state where the wizard is on screen while the engine
 * still believes the user declined it -- two truths about one decision. So the
 * entry points clear it first, over HTTP, and only navigate once the engine
 * has acknowledged.
 *
 * WHY THE PROBE IS AWAITED HERE. `setupRefusal()` reads the capability store,
 * which is populated by ONE health GET fired from the root layout's onMount.
 * A caller that reads the refusal synchronously in the same tick loses that
 * race and gets "daemon not identified yet" -- a sentence about the probe, not
 * about the daemon. `capabilities.probe()` is memoized on success, so awaiting
 * it here costs nothing after the first call and removes the race entirely.
 * This is the same order `resolveFirstRun()` uses.
 *
 * NAVIGATION IS INJECTED, never imported. `goto` comes from `$app/navigation`,
 * which only exists inside a SvelteKit runtime; taking it as an argument keeps
 * this module executable under node:test, so the rule is under test rather
 * than under a component mount.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 runSetup awaits the capability probe before reading the refusal,
 *     so a click in the first tick after load is not answered with
 *     "daemon not identified yet".
 *     [if] runSetup reads setupRefusal() before awaiting probe() [then ⛔️] broken
 *   ✔︎ ✅ 🎯 a refused setup surface never navigates and never issues a
 *     request; the refusal sentence is returned verbatim for the caller to
 *     show.
 *     [if] a legacy boot lands on /setup anyway [then ⛔️] broken
 *   ✔︎ ✅ 🎯 a failed re-arm does NOT navigate. Landing on the wizard while
 *     the engine still holds the dismissal is the split-truth this avoids.
 *     [if] a 500 from the dismissal write still navigates [then ⛔️] broken
 *   ✔︎ ✅ 🎯 success navigates to exactly SETUP_ROUTE and reports no error.
 *     [if] the destination is spelled as a literal at a call site [then ⛔️] broken
 *   ✔︎ ✅ 🎯 runSetupBlocked disables the control only on a FINAL refusal, so
 *     an unfinished probe never reads as "you may not".
 *     [if] an unknown flavor disables the button [then ⛔️] broken
 */

import { capabilities } from '../api/capabilities.svelte';
import { openSetupOverlay } from './overlay.svelte';
import { finalSetupRefusal, setupRefusal } from './setup-api';

/** The wizard's route. Spelled once so three entry points cannot disagree.
 *
 * It is now a DOOR, not a destination: /setup opens the setup overlay and
 * hands the browser straight on to SETUP_HOST_ROUTE, because the wizard is an
 * overlay over the live app rather than a page of its own. Deep links, agent
 * flows and older bookmarks all still land somewhere real. */
export const SETUP_ROUTE = '/setup';

/** The page the setup overlay is drawn OVER. The performance view is the
 * app's actual front door, so the ask arrives with its subject behind it
 * instead of over an empty table. */
export const SETUP_HOST_ROUTE = '/performance';

/** The label every entry point shows, so they are recognisably the same door. */
export const RUN_SETUP_LABEL = 'Run setup';

/** Primary boot-gate CTA for empty libraries (issue #2722). */
export const IMPORT_MUSIC_LABEL = 'Import your music';

/**
 * The hover explanation, per the house rule that a control says what it does
 * and what it will change. Used as the enabled-state `title`; the refusal
 * sentence replaces it when the daemon does not offer setup.
 */
export const RUN_SETUP_TITLE =
	'Open the first-run setup wizard at /setup. Clears the engine-side "dismissed" flag first, so the library import is offered again.';

/**
 * Why the entry-point control should be DISABLED, or null when it should not.
 *
 * Deliberately narrower than `setupRefusal()`. That function has two very
 * different "no" answers folded into one string type: the daemon does not
 * serve setup (final), and the health probe has not answered yet (temporary).
 * Disabling a button on the second one is a hidden default -- it turns "I do
 * not know yet" into "you may not", and on a fast click after load that is
 * simply wrong. So only the FINAL refusal disables; while the flavor is
 * unknown the button stays live and `runSetup` awaits the probe and answers
 * for real.
 */
export function runSetupBlocked(): string | null {
	return finalSetupRefusal();
}

/**
 * Re-arm the wizard engine-side and navigate to it.
 *
 * Returns null when the caller is now on /setup, or the sentence explaining
 * why it is not. Never throws: every failure here has a sentence a user can
 * read, and a caller that had to catch would only translate it back into one.
 *
 * @param navigate the router's navigation function, normally `goto`.
 */
export async function runSetup(navigate: (path: string) => unknown): Promise<string | null> {
	await capabilities.probe();
	const refusal = setupRefusal();
	if (refusal !== null) return refusal;
	// Loaded on click, not imported: this module sits in the root layout's
	// first-paint graph (Settings and preflight entry points), and the wizard
	// store with its copy rules is only needed once someone asks for setup.
	// The overlay that renders it is lazy for the same reason.
	const { setupWizard } = await import('./wizard.svelte');
	await setupWizard.reopen();
	// reopen() records the server's message rather than throwing, so the
	// navigation gate is that field and not an exception.
	if (setupWizard.error !== null) return setupWizard.error;
	// Raise the overlay BEFORE navigating. /setup would raise it too, but a
	// caller that is already on the host route never navigates at all, and an
	// entry point whose only effect was a no-op goto is a dead button.
	openSetupOverlay();
	await navigate(SETUP_ROUTE);
	return null;
}
