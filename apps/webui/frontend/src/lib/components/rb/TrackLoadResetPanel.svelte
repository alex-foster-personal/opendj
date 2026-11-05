<script lang="ts">
	// DECKGAP-7691 (pin 251822d6104b). One list, every item that can return to neutral when
	// a track loads onto a deck. Opened from the stem-row RST chip.
	import { TRACK_LOAD_RESET_ITEMS, TRACK_LOAD_RESET_WARNING } from '$lib/rb/track-load-reset';
	import {
		closeTrackLoadReset,
		resetDeckNowCommand,
		trackLoadResetCommand,
		trackLoadResetDeck,
		trackLoadResetEnabled,
		trackLoadResetFocus,
		trackLoadResetOpen,
		trackLoadResetTrigger
	} from '$lib/rb/track-load-reset.svelte';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';

	let panelEl: HTMLDivElement | undefined = $state();
	const open = $derived(trackLoadResetOpen());
	const focusId = $derived(trackLoadResetFocus());
	const deck = $derived(trackLoadResetDeck());

	function onWindowPointerDown(event: PointerEvent): void {
		if (!open) return;
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (panelEl?.contains(target)) return;
		if (target instanceof Element && target.closest('[data-track-load-reset-open]') !== null) return;
		closeTrackLoadReset();
	}

	function onWindowKeyDown(event: KeyboardEvent): void {
		if (!open || event.key !== 'Escape') return;
		event.preventDefault();
		const back = trackLoadResetTrigger();
		closeTrackLoadReset();
		back?.focus();
	}
</script>

<svelte:window onpointerdown={onWindowPointerDown} onkeydown={onWindowKeyDown} />

{#if open}
	<div
		class="panel"
		role="dialog"
		aria-label="Reset when a new track loads"
		data-testid="track-load-reset-panel"
		bind:this={panelEl}
		use:triggerFloatingAction={{ getTrigger: () => trackLoadResetTrigger(), preferred: 'above', gap: 4 }}
	>
		<h2>Reset when a new track loads</h2>
		<p class="warn">{TRACK_LOAD_RESET_WARNING}</p>
		<ul>
			{#each TRACK_LOAD_RESET_ITEMS as item (item.id)}
				<li class:focus={item.id === focusId}>
					<label>
						<input
							type="checkbox"
							id={`track-load-reset-${item.id}`}
							data-testid={`track-load-reset-${item.id}`}
							checked={trackLoadResetEnabled(item.id)}
							onchange={(event) => void runPerformanceCommandFromUi(trackLoadResetCommand(item.id, event.currentTarget.checked))}
						/>
						<span class="name">Reset {item.label}</span>
					</label>
					<p>{item.detail}</p>
				</li>
			{/each}
		</ul>
		<div class="foot">
			<p class="scope">Same choice on every deck. Saved in this browser.</p>
			{#if deck !== null}
				<button
					type="button"
					class="now"
					data-testid="track-load-reset-now"
					title={`Apply the ticked resets to deck ${deck} now, without waiting for a load`}
					onclick={() => void runPerformanceCommandFromUi(resetDeckNowCommand(deck))}
				>
					Reset now
				</button>
			{/if}
		</div>
	</div>
{/if}

<style>
	.panel {
		position: fixed;
		z-index: 1000;
		width: 280px;
		background: var(--rb-panel, #14181f);
		border: 1px solid var(--rb-border, #2c3440);
		border-radius: 4px;
		padding: 8px 10px 10px;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.45);
		color: var(--rb-text, #e7ecf2);
		font-family: var(--rb-font);
	}
	h2 {
		margin: 0 0 6px;
		font-size: 12px;
		font-weight: 700;
		letter-spacing: 0.02em;
	}
	.warn {
		margin: 0 0 8px;
		padding: 6px 7px;
		border: 1px solid #f2b84b;
		border-radius: 2px;
		background: color-mix(in srgb, #f2b84b 12%, transparent);
		color: #f2b84b;
		font-size: 11px;
		line-height: 1.35;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	li {
		padding: 4px 4px 2px;
		border-radius: 2px;
	}
	li.focus {
		background: color-mix(in srgb, var(--rb-accent, #49c8ff) 14%, transparent);
	}
	label {
		display: flex;
		align-items: center;
		gap: 6px;
		cursor: pointer;
		font-size: 12px;
		font-weight: 650;
	}
	.name {
		line-height: 1.2;
	}
	li p {
		margin: 2px 0 0 20px;
		font-size: 10px;
		line-height: 1.35;
		color: var(--rb-text-dim, #9aa4b2);
	}
	.foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin: 8px 0 0;
	}
	.scope {
		margin: 0;
		font-size: 10px;
		color: var(--rb-text-dim, #9aa4b2);
	}
	/* DECKGAP-7691h: secondary, not a call to action. Dark fill, 1px outline, never the accent. */
	.now {
		flex: none;
		margin-left: auto;
		padding: 2px 7px;
		border: 1px solid var(--rb-border, #2c3440);
		border-radius: 2px;
		background: transparent;
		color: var(--rb-text-dim, #9aa4b2);
		font: inherit;
		font-size: 10px;
		line-height: 1.3;
		cursor: pointer;
	}
	.now:hover {
		border-color: var(--rb-text-dim, #9aa4b2);
		color: var(--rb-text, #e7ecf2);
	}
	.now:focus-visible {
		outline: 1px solid var(--rb-text, #e7ecf2);
		outline-offset: 1px;
	}
</style>
