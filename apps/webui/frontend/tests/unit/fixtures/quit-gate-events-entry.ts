/**
 * Shared-graph entry for quit-gate + events-bus contract tests (issue #6262).
 *
 * loadTypeScriptModule bundles each entry independently; quit-gate.ts and
 * events-bus.ts loaded separately would leave subscribe(TOPIC_SHELL_QUIT) on a
 * different singleton than the fake socket the test drives.
 */
export {
	ensureEarlyShellQuitGate,
	resetShellQuitGateForTests
} from '$lib/shell/quit-gate';
export * as eventsBus from '$lib/api/events-bus';
