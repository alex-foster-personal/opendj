<script lang="ts">
	// Says the Rust engine is playing this page (NAE-13). Shown only in that
	// mode, so a DJ can never mistake which engine the decks run on, and it
	// carries the engine's start-up error when there is one. Fixed-position
	// and outside the grid, like the AutoPlay stall banner.
	import { onMount } from 'svelte';
	import { watchRustInert } from '$lib/audio-engine/rust-inert';
	import { ensureRustEngine, initRustMode, rustMode } from '$lib/audio-engine/rust-mode.svelte';

	let inert = $state<ReturnType<typeof watchRustInert> | null>(null);
	let held = $state<string[]>([]);

	onMount(() => {
		initRustMode();
		if (!rustMode.enabled) return;
		// Start the engine with the page, so the first load does not wait on
		// it. A failure shows here; commands then fail with the same reason.
		ensureRustEngine().catch(() => {});
		// Gray out, page-wide, the controls this engine cannot play yet.
		inert = watchRustInert(document.body);
		return () => inert?.stop();
	});

	$effect(() => {
		// What is unsupported changes when the engine says hello and when it
		// first reports key lock.
		void rustMode.notBuilt;
		void rustMode.keyLock;
		if (inert !== null) held = inert.refresh();
	});

	const label = $derived(
		rustMode.status === 'connected'
			? 'Rust engine'
			: rustMode.status === 'error'
				? 'Rust engine: not running'
				: 'Rust engine: starting'
	);
	const title = $derived(
		rustMode.status === 'error'
			? `The Rust audio engine could not start: ${rustMode.error ?? 'no reason given'}. Switch back in Settings > Audio engine.`
			: `Decks play through odj-audio (${rustMode.clock} clock), not Web Audio. Switch in Settings > Audio engine.` +
				(held.length > 0 ? ` Not available yet, grayed out: ${held.join(', ')}.` : '')
	);
</script>

{#if rustMode.enabled}
	<div
		class="rust-engine-badge"
		class:is-error={rustMode.status === 'error'}
		role="status"
		{title}
		data-engine-status={rustMode.status}
	>
		{label}
	</div>
{/if}

<style>
	.rust-engine-badge {
		position: fixed;
		/* The empty middle of the bottom status bar: the corners hold the
		   feedback and help buttons. */
		left: 50%;
		bottom: 1px;
		transform: translateX(-50%);
		z-index: 40;
		padding: 1px 8px;
		border: 1px solid color-mix(in srgb, var(--rb-accent) 60%, var(--rb-border));
		border-radius: 3px;
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		line-height: 14px;
		pointer-events: auto;
	}

	.rust-engine-badge.is-error {
		border-color: color-mix(in srgb, var(--rb-red) 65%, var(--rb-border));
		color: var(--rb-red);
	}
</style>
