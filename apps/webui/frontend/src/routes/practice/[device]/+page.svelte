<script lang="ts">
	/**
	 * /practice/<device>: practice mode for a hardware layout skin (ADR-0137).
	 * ?target=<control id> starts a drill: the named control is outlined, and the page
	 * records reach time on a hit and counts presses on any other control as misses.
	 * Agent parity: `opendj practice <device>` opens this route; the layout it renders is
	 * GET /api/v1/practice/devices/<device>.
	 */
	import { page } from '$app/state';
	import DeviceReplica from '$lib/components/rb/practice/DeviceReplica.svelte';
	import { fetchPanelLayout, practiceMapFor } from '$lib/components/rb/practice/practice-devices';
	import { installPracticeSession } from '$lib/components/rb/practice/practice-session';
	import { onMount } from 'svelte';

	import type { PanelLayout } from '$lib/components/rb/practice/practice-bindings';

	const device = $derived(page.params.device ?? '');
	const target = $derived(page.url.searchParams.get('target'));

	let layout = $state<PanelLayout | null>(null);
	let loadError = $state<string | null>(null);
	let startedMs = $state(0);
	let reachMs = $state<number | null>(null);
	let misses = $state(0);

	function restart(): void {
		startedMs = performance.now();
		reachMs = null;
		misses = 0;
	}

	// A new device or target is a new drill: reload, and start timing only once the panel is up.
	$effect(() => {
		const wanted = device;
		void target;
		layout = null;
		loadError = null;
		let live = true;
		fetchPanelLayout(wanted).then(
			(loaded) => {
				if (!live) return;
				layout = loaded;
				restart();
			},
			(err: unknown) => {
				if (live) loadError = String(err);
			}
		);
		return () => {
			live = false;
		};
	});

	// Replica controls dispatch performance commands, which need a live command session.
	onMount(() => installPracticeSession());

	function onHit(controlId: string): void {
		if (target === null || reachMs !== null) return;
		if (controlId === target) reachMs = Math.round(performance.now() - startedMs);
		else misses += 1;
	}
</script>

<svelte:head><title>Practice - {device}</title></svelte:head>

<main class="practice" data-testid="practice-page">
	{#if loadError !== null}
		<p class="error" role="alert">Could not open practice for {device}: {loadError}</p>
	{:else if layout === null}
		<p>Loading the {device} panel...</p>
	{:else}
		<header>
			<h1>Practice: {layout.display_name}</h1>
			<p class="note">
				Schematic replica. Works with {layout.device.toUpperCase()} MIDI. Hover any control to see what
				it does.
			</p>
			{#if target !== null}
				<p class="drill">
					Drill: reach <strong>{target}</strong>.
					<span title="Milliseconds from drill start to pressing the target control"
						>reach {reachMs ?? '-'} ms</span
					>
					<span title="Presses on other controls before the target">misses {misses}</span>
					<button type="button" title="Start the drill timer again" onclick={restart}
						>Restart</button
					>
				</p>
			{/if}
		</header>
		<DeviceReplica {layout} map={practiceMapFor(layout)} {target} {onHit} />
	{/if}
</main>

<style>
	.practice {
		padding: 16px;
		max-width: 1400px;
		margin: 0 auto;
		background: #121418;
		color: #e8ebf0;
		min-height: 100vh;
		box-sizing: border-box;
	}
	h1 {
		font-size: 16px;
		margin: 0 0 4px;
	}
	.note,
	.drill {
		font-size: 12px;
		margin: 0 0 8px;
		display: flex;
		gap: 12px;
		align-items: center;
	}
	.error {
		color: #ff8a8a;
	}
</style>
