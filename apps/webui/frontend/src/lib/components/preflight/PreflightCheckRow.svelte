<script lang="ts">
	/**
	 * One row of PREFLIGHT-01/02's boot gate (issue #771, issue #2589).
	 *
	 * Split out of PreflightScreen.svelte as a pure presentational leaf with
	 * NO lifecycle hooks of its own (no onMount/onDestroy/$effect): that
	 * parent's polling loop registers onDestroy, which only makes sense for a
	 * component that gets mounted-then-later-destroyed in a browser, and
	 * throws under `svelte/server`'s one-shot `render()` (there is nothing to
	 * destroy after a single server render). This row has nothing like that,
	 * so it CAN be mounted through the real Svelte SSR renderer -- which is
	 * how issue #2589's dismissed-empty "Run setup" control is proven to
	 * actually render, rather than merely grepped for in source.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ 🎯 a `library-attached` row that is `pending` because setup was
	 *     dismissed with an empty library shows a real, working "Run setup"
	 *     control -- never a dead end with only prose remediation.
	 *     [if] this state renders no control, or a disabled/inert one, while
	 *     the daemon is reachable [then ⛔️] broken
	 *   ✔︎ 🎯 every other row (including every OTHER `pending` row, e.g.
	 *     audio-access with nothing sampleable) renders no such control --
	 *     "Run setup" only ever appears where it would do something.
	 *     [if] any check id other than library-attached offers Run setup
	 *     [then ⛔️] broken
	 */
	import type { PreflightCheck } from '$lib/api';
	import { checkOffersRunSetup } from '$lib/preflight/preflight-actions';
	import { RUN_SETUP_LABEL, runSetup, runSetupBlocked } from '$lib/setup/run-setup';
	import PreflightCheckRowView from './PreflightCheckRowView.svelte';

	let {
		check,
		navigate,
		mode = 'boot',
		label: labelOverride,
		detail: detailOverride
	}: {
		check: PreflightCheck;
		navigate?: ((path: string) => unknown) | undefined;
		mode?: 'boot' | 'admin';
		label?: string;
		detail?: string;
	} = $props();

	let setupBusy = $state(false);
	let setupError = $state<string | null>(null);

	const offersRunSetup = $derived(checkOffersRunSetup(check));

	async function handleRunSetup(): Promise<void> {
		if (!navigate || setupBusy) return;
		setupBusy = true;
		setupError = await runSetup(navigate);
		setupBusy = false;
	}
</script>

<PreflightCheckRowView
	{check}
	{mode}
	label={labelOverride}
	detail={detailOverride}
	{offersRunSetup}
	runSetupLabel={RUN_SETUP_LABEL}
	{setupBusy}
	{setupError}
	isRunSetupBlocked={() => runSetupBlocked() !== null}
	onRunSetup={() => void handleRunSetup()}
/>
