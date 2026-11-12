<!--
	Lighting overrides panel (issue #9899, spec item L56). One control per override; every
	control is one POST of /api/v1/lighting/overrides (the same endpoint the CLI and MCP drive)
	and every control state is rendered from polled server state, never optimistic local state.
	Blackout needs a 300 ms hold so a stray click cannot black out a room.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { API_BASE } from '$lib/api';
	import {
		BLACKOUT_HOLD_MS,
		MOODS,
		OVERRIDES_POLL_MS,
		STROBE_SPEEDS,
		createHoldGate,
		createOverridesPoller,
		postOverrides,
		type FetchLike,
		type LightingOverrides,
		type OverridesPatch
	} from '$lib/lighting/overrides-client';

	let {
		baseUrl = API_BASE,
		fetchFn = globalThis.fetch.bind(globalThis) as FetchLike
	}: {
		baseUrl?: string;
		fetchFn?: FetchLike;
	} = $props();

	let overrides = $state<LightingOverrides | null>(null);
	let error = $state<string | null>(null);
	let posting = $state(false);
	let holding = $state(false);

	const clock = {
		setTimeout: (fn: () => void, ms: number) => globalThis.setTimeout(fn, ms),
		clearTimeout: (h: unknown) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>)
	};

	const gate = createHoldGate({
		holdMs: BLACKOUT_HOLD_MS,
		clock,
		onConfirm: () => {
			holding = false;
			void send({ blackout: true });
		}
	});

	const disabled = $derived(posting || overrides === null);

	async function send(patch: OverridesPatch) {
		posting = true;
		try {
			overrides = await postOverrides(fetchFn, baseUrl, patch);
			error = null;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			posting = false;
		}
	}

	function pressBlackout() {
		gate.press();
		holding = gate.holding;
	}
	function releaseBlackout() {
		gate.release();
		holding = false;
	}
	function cancelBlackout() {
		gate.cancel();
		holding = false;
	}
	function blackoutKeydown(e: KeyboardEvent) {
		if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) {
			e.preventDefault();
			pressBlackout();
		}
	}
	function blackoutKeyup(e: KeyboardEvent) {
		if (e.key === ' ' || e.key === 'Enter') releaseBlackout();
	}

	onMount(() => {
		const poller = createOverridesPoller({
			fetchFn,
			baseUrl,
			intervalMs: OVERRIDES_POLL_MS,
			clock,
			onState: (s) => {
				overrides = s;
				error = null;
			},
			onError: (err) => {
				error = `Lighting overrides unavailable: ${err instanceof Error ? err.message : String(err)}`;
			}
		});
		poller.start();
		return () => {
			poller.stop();
			gate.cancel();
		};
	});
</script>

<section class="overrides" aria-label="Lighting overrides">
	<h2>Overrides</h2>
	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	{#if overrides?.blackout}
		<button
			class="blackout on"
			aria-pressed="true"
			{disabled}
			onclick={() => send({ blackout: false })}>Blackout ON - click to restore</button
		>
	{:else}
		<button
			class="blackout motion-essential"
			class:holding
			style="--hold-ms: {BLACKOUT_HOLD_MS}ms"
			aria-pressed="false"
			{disabled}
			onpointerdown={pressBlackout}
			onpointerup={releaseBlackout}
			onpointerleave={cancelBlackout}
			onpointercancel={cancelBlackout}
			onkeydown={blackoutKeydown}
			onkeyup={blackoutKeyup}>BLACKOUT - hold</button
		>
	{/if}

	<label class="row">
		<span>Master dimmer {overrides ? Math.round(overrides.dimmer * 100) : '-'}%</span>
		<input
			type="range"
			min="0"
			max="1"
			step="0.01"
			value={overrides?.dimmer ?? 0}
			{disabled}
			onchange={(e) => send({ dimmer: Number((e.currentTarget as HTMLInputElement).value) })}
		/>
	</label>

	<div class="row">
		<span>Colour</span>
		<button
			aria-pressed={overrides?.colour === 'auto'}
			{disabled}
			onclick={() => send({ colour: 'auto' })}>AUTO</button
		>
		<input
			type="color"
			aria-label="Fixed colour"
			value={overrides && overrides.colour !== 'auto' ? overrides.colour : '#ffffff'}
			{disabled}
			onchange={(e) =>
				send({ colour: (e.currentTarget as HTMLInputElement).value.toLowerCase() as `#${string}` })}
		/>
	</div>

	<div class="row" role="group" aria-label="Strobe">
		<span>Strobe</span>
		{#each STROBE_SPEEDS as speed (speed)}
			<button
				aria-pressed={overrides?.strobe === speed}
				{disabled}
				onclick={() => send({ strobe: speed })}>{speed.toUpperCase()}</button
			>
		{/each}
	</div>

	<div class="row" role="group" aria-label="Mood">
		<span>Mood</span>
		{#each MOODS as mood (mood)}
			<button aria-pressed={overrides?.mood === mood} {disabled} onclick={() => send({ mood })}
				>{mood.toUpperCase()}</button
			>
		{/each}
	</div>

	<div class="row">
		<span>Ambient</span>
		<button
			aria-pressed={overrides?.ambient ?? false}
			{disabled}
			onclick={() => overrides && send({ ambient: !overrides.ambient })}
			>{overrides?.ambient ? 'ON' : 'OFF'}</button
		>
	</div>
</section>

<style>
	.overrides {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		max-width: 32rem;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.error {
		color: #ff6b6b;
	}
	button[aria-pressed='true'] {
		outline: 2px solid currentColor;
	}
	.blackout {
		position: relative;
		overflow: hidden;
		padding: 1.25rem;
		font-size: 1.25rem;
		font-weight: 700;
		color: #fff;
		background: #8b0000;
		border: 3px solid #ff3030;
		border-radius: 0.5rem;
	}
	.blackout::after {
		content: '';
		position: absolute;
		inset: 0;
		width: 0;
		background: rgba(0, 0, 0, 0.6);
	}
	.blackout.holding::after {
		width: 100%;
		transition: width var(--hold-ms) linear;
	}
	.blackout.on {
		background: #000;
	}
</style>
