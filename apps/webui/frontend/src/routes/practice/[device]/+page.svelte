<script lang="ts">
	/**
	 * /practice/<device>: practice mode for a hardware layout skin (ADR-0137).
	 * ?target=<control id> starts a drill: the named control is outlined, and the page
	 * records reach time on a hit and counts presses on any other control as misses, from the
	 * replica or the real controller (hardware hits, #10251).
	 * Agent parity: `opendj practice <device>` opens this route; the layout it renders is
	 * GET /api/v1/practice/devices/<device>.
	 */
	import {
		createPracticeLayersIpc,
		installPracticeLayersIpc
	} from '$lib/components/rb/practice/practice-pad-layers-ipc';
	import { page } from '$app/state';
	import DeviceReplica from '$lib/components/rb/practice/DeviceReplica.svelte';
	import { fetchPanelLayout, practiceMapFor } from '$lib/components/rb/practice/practice-devices';
	import { createDrill, type DrillState } from '$lib/components/rb/practice/practice-drill';
	import {
		installDrillHardwareHits,
		installPracticeSession
	} from '$lib/components/rb/practice/practice-session';
	import { onMount } from 'svelte';

	import type { PanelLayout } from '$lib/components/rb/practice/practice-bindings';

	const device = $derived(page.params.device ?? '');
	const target = $derived(page.url.searchParams.get('target'));

	let replica = $state<ReturnType<typeof DeviceReplica> | undefined>(undefined);
	let layout = $state<PanelLayout | null>(null);
	let loadError = $state<string | null>(null);
	let drill = createDrill(null);
	let drillState = $state<DrillState>(drill.state);

	function restart(): void {
		drillState = drill.restart(performance.now());
	}

	// A new device or target is a new drill: reload, and start timing only once the panel is up.
	$effect(() => {
		const wanted = device;
		const drilled = target;
		layout = null;
		loadError = null;
		let live = true;
		fetchPanelLayout(wanted).then(
			(loaded) => {
				if (!live) return;
				layout = loaded;
				drill = createDrill(drilled);
				drillState = drill.start(performance.now());
			},
			(err: unknown) => {
				if (live) loadError = String(err);
			}
		);
		return () => {
			live = false;
		};
	});

	$effect(() => {
		const owner = replica;
		if (owner === undefined) return;
		return installPracticeLayersIpc(
			createPracticeLayersIpc({
				dispatch: (cmd) => owner.dispatchLayer(cmd),
				query: () => owner.queryLayers()
			})
		);
	});

	// Replica controls dispatch performance commands, which need a live command session.
	onMount(() => installPracticeSession());

	function onHit(controlId: string): void {
		drillState = drill.press(controlId, performance.now());
	}

	// Hardware hits: only while drilling, only from this route's device; no ?target=, no subscription.
	$effect(() => {
		if (layout === null || target === null) return;
		return installDrillHardwareHits(layout, practiceMapFor(layout), onHit);
	});
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
						>reach {drillState.reachMs ?? '-'} ms</span
					>
					<span title="Presses on other controls before the target">misses {drillState.misses}</span
					>
					<button type="button" title="Start the drill timer again" onclick={restart}
						>Restart</button
					>
				</p>
			{/if}
		</header>
		<DeviceReplica bind:this={replica} {layout} map={practiceMapFor(layout)} {target} {onHit} />
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
