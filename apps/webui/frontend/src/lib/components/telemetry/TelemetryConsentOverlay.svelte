<script lang="ts">
	/**
	 * First-launch diagnostics consent (OBS-05). The text here is the
	 * tester-facing rendering of docs/legal/test-user-terms.md; keep the two
	 * in step and bump TERMS_VERSION in apps/shared/telemetry/consent.py when
	 * either changes materially.
	 */
	import {
		currentConsent,
		isConsentDialogOpen,
		subscribeConsentDialogOpen
	} from '$lib/telemetry-consent-state';
	import TelemetryConsentOverlayView from './TelemetryConsentOverlayView.svelte';

	interface Props {
		/** Records the answer with the engine; `answerConsent` in production.
		 * A prop rather than an import so this component never imports the
		 * module that mounts it (that pair would be an import cycle). */
		answer: (decision: 'accepted' | 'declined') => Promise<unknown>;
	}

	let { answer: recordAnswer }: Props = $props();

	let open = $state(false);
	let busy = $state(false);
	let failure = $state<string | null>(null);

	$effect(() => {
		const sync = (): void => {
			open = isConsentDialogOpen();
		};
		sync();
		return subscribeConsentDialogOpen(sync);
	});

	const termsVersion = $derived(currentConsent()?.terms_current_version ?? '');
	const replayOffered = $derived(currentConsent()?.replay_loader_url !== null);

	async function answer(decision: 'accepted' | 'declined'): Promise<void> {
		busy = true;
		failure = null;
		try {
			await recordAnswer(decision);
		} catch (error) {
			failure = error instanceof Error ? error.message : String(error);
		} finally {
			busy = false;
		}
	}
</script>

<TelemetryConsentOverlayView
	{open}
	{busy}
	{failure}
	{termsVersion}
	{replayOffered}
	onAnswer={(decision) => void answer(decision)}
/>
