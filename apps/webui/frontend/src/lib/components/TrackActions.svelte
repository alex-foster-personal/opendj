<script lang="ts">
	import { onMount } from 'svelte';
	import { REVEAL_TRACK_TITLE, runRevealTracks } from '$lib/components/rb/browser/track-context-menu';
	import {
		fetchHandoffApps,
		HANDOFF_BUTTONS,
		handoffButtonState,
		openTrackInApp,
		type HandoffApp,
		type HandoffAppId
	} from '$lib/rb/api-track-handoff';
	import TrackActionsView from './TrackActionsView.svelte';

	let { stableId }: { stableId: string } = $props();

	// HANDOFF-01..03: each button is enabled only when the engine found that
	// app on this machine; otherwise its tooltip says why, never a dead click.
	let probe = $state<HandoffApp[] | 'loading' | { error: string }>('loading');

	onMount(() => {
		fetchHandoffApps()
			.then((apps) => (probe = apps))
			.catch((err: unknown) => (probe = { error: err instanceof Error ? err.message : String(err) }));
	});

	// The click's result shows inline beside the buttons (a status line, not a
	// toast, so this component stays off the stores.svelte hub).
	let outcome = $state<{ text: string; error: boolean } | null>(null);

	async function openIn(app: HandoffAppId, label: string): Promise<void> {
		outcome = null;
		try {
			await openTrackInApp(stableId, app);
			outcome = { text: `Sent to ${label.replace('Open in ', '')}`, error: false };
		} catch (err) {
			outcome = { text: err instanceof Error ? err.message : String(err), error: true };
		}
	}

	const handoffButtons = $derived(
		HANDOFF_BUTTONS.map(({ app, label }) => ({ app, label, ...handoffButtonState(app, probe) }))
	);
</script>

<TrackActionsView
	revealTitle={REVEAL_TRACK_TITLE}
	onReveal={() => void runRevealTracks([stableId])}
	buttons={handoffButtons}
	onOpenIn={(app, label) => void openIn(app, label)}
	{outcome}
/>
