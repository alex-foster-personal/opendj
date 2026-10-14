<script lang="ts">
	// CMDK-06: small deck rings above the Cmd K bar's top-right corner, a
	// reminder rather than a control panel (the maintainer, Thu 8 Oct 2026). A short
	// arrow circles a ring clockwise while its deck plays, the master ring is
	// yellow, the deck Enter loads to is filled, and a ring pulses red once its
	// playing deck is within RUNOUT_BEATS of its outro (or end).
	import { deckStates as decks, DECK_IDS } from '$lib/rb/audio-engine.svelte';
	import type { DeckId } from '$lib/rb/deck-id';
	import { isRunningOut, RUNOUT_BEATS } from '$lib/rb/deck-runout';

	let { target, onPick }: { target: DeckId; onPick: (id: DeckId) => void } = $props();

	function runningOut(id: DeckId): boolean {
		const d = decks[id];
		return isRunningOut({
			stable_id: d.stable_id,
			playing: d.playing,
			position_ms: d.position_ms,
			duration_ms: d.duration_ms,
			bpm: d.bpm,
			pitch: d.pitch,
			phrases: d.anlz?.phrases ?? null,
			beats: d.anlz?.beatgrid.beats ?? null
		});
	}

	function describe(id: DeckId): string {
		const d = decks[id];
		const what = d.stable_id === null ? 'empty' : `"${d.title ?? 'untitled'}", ${d.playing ? 'playing' : 'paused'}`;
		const tail = runningOut(id) ? `; within ${RUNOUT_BEATS} beats of its outro` : '';
		return `Deck ${id}${d.is_master ? ' (master)' : ''}: ${what}${tail}. Click or Alt+${id} to load the pick here.`;
	}
</script>

<div class="cmdk-rings" role="group" aria-label="Decks" data-testid="command-bar-decks">
	{#each DECK_IDS as id (id)}
		{@const d = decks[id]}
		<button
			type="button"
			class="ring-btn"
			class:on={target === id}
			class:master={d.is_master}
			class:empty={d.stable_id === null}
			class:playing={d.stable_id !== null && d.playing}
			class:runout={runningOut(id)}
			aria-pressed={target === id}
			aria-label={describe(id)}
			title={describe(id)}
			data-testid={`command-bar-deck-${id}`}
			onmousedown={(event) => event.preventDefault()}
			onclick={() => onPick(id)}
		>
			<svg viewBox="0 0 20 20" aria-hidden="true">
				<circle class="ring" cx="10" cy="10" r="8.6" />
				<g class="spin">
					<path d="M10 1.4A8.6 8.6 0 0 1 17.4 5.6" />
					<path class="head" d="M18.6 4.4 17.9 7.6 15.2 5.9Z" />
				</g>
				<text class="num" x="10" y="13.2" text-anchor="middle">{id}</text>
			</svg>
		</button>
	{/each}
</div>

<style>
	.cmdk-rings {
		display: flex;
		gap: 6px;
	}
	.ring-btn {
		width: 20px;
		height: 20px;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: transparent;
		color: var(--rb-text-dim, #8a94a3);
		cursor: pointer;
	}
	svg {
		width: 20px;
		height: 20px;
		display: block;
		overflow: visible;
	}
	.ring {
		fill: none;
		stroke: var(--rb-border, #2c323c);
		stroke-width: 1.4;
	}
	.num {
		font: 600 9px var(--rb-font, system-ui, sans-serif);
		fill: currentColor;
	}
	.spin {
		fill: none;
		stroke: currentColor;
		stroke-width: 1.4;
		stroke-linecap: round;
		opacity: 0;
		transform-origin: 10px 10px;
	}
	.spin .head {
		fill: currentColor;
		stroke: none;
	}
	.empty {
		opacity: 0.55;
	}
	.empty .ring {
		stroke-dasharray: 2 2.4;
	}
	.playing .spin {
		opacity: 0.75;
	}
	.master {
		color: var(--rb-master, #c9b35a);
	}
	.master .ring {
		stroke: color-mix(in srgb, var(--rb-master, #c9b35a) 55%, transparent);
	}
	.on .ring {
		fill: color-mix(in srgb, var(--rb-text, #e6e9ee) 14%, transparent);
		stroke: var(--rb-text-dim, #8a94a3);
	}
	.on:not(.master) {
		color: var(--rb-text, #e6e9ee);
	}
	.runout {
		color: var(--rb-red, #d0342c);
	}
	.runout .ring {
		stroke: var(--rb-red, #d0342c);
	}
	.ring-btn:focus-visible {
		outline: 2px solid var(--rb-cmdk-pick, #a46ef0);
		outline-offset: 2px;
	}
	@media (prefers-reduced-motion: no-preference) {
		.playing .spin {
			animation: cmdk-spin 2.4s linear infinite;
		}
		.runout {
			animation: cmdk-runout 0.9s ease-in-out infinite;
		}
	}
	@keyframes cmdk-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@keyframes cmdk-runout {
		0%,
		100% {
			box-shadow: 0 0 0 0 color-mix(in srgb, var(--rb-red, #d0342c) 0%, transparent);
		}
		50% {
			box-shadow: 0 0 0 4px color-mix(in srgb, var(--rb-red, #d0342c) 35%, transparent);
		}
	}
</style>
