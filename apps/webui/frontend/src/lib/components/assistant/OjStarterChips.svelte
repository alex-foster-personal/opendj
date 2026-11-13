<script lang="ts">
	/**
	 * OJ's starter chips for an empty agent sidebar (issue #14040). Props only: AgentSidebar.svelte
	 * picks the list by route (oj-starters.ts) and the motion through ojMotionPolicy. A click hands
	 * the chip's text to `onpick` (the container puts it in the composer); nothing is sent. Chips
	 * enter with a CSS animation-delay stagger, so no JS timer runs; still motion means no entry.
	 */
	import { ojStarterStaggerMs, type OjStarter } from '$lib/assistant/oj-starters';
	import type { OjMotion } from './oj-motion-policy';

	let {
		starters,
		motion,
		onpick
	}: { starters: readonly OjStarter[]; motion: OjMotion; onpick: (text: string) => void } =
		$props();

	const staggerMs = $derived(ojStarterStaggerMs(motion));
</script>

{#if starters.length > 0}
	<div class="oj-starters" role="group" aria-label="Things to ask OJ">
		{#each starters as starter, index (starter.id)}
			<button
				type="button"
				class="oj-starter"
				class:enter={staggerMs > 0}
				style:animation-delay={staggerMs > 0 ? `${index * staggerMs}ms` : undefined}
				data-oj-starter={starter.id}
				data-oj-starter-kind={starter.kind}
				title="Put this in the message box to edit or send"
				onclick={() => onpick(starter.text)}
			>
				{starter.text}
			</button>
		{/each}
	</div>
{/if}

<style>
	.oj-starters {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.oj-starter {
		font: inherit;
		font-size: 0.92em;
		color: var(--sb-muted, var(--muted));
		background: transparent;
		border: 1px solid var(--sb-line, var(--border));
		border-radius: 99px;
		padding: 3px 10px;
		text-align: left;
		cursor: pointer;
	}
	.oj-starter:hover,
	.oj-starter:focus-visible {
		color: var(--sb-fg, var(--fg));
		background: var(--sb-user-bg, var(--surface-raised));
	}
	.oj-starter.enter {
		animation: oj-starter-enter 180ms ease-out both;
	}
	@keyframes oj-starter-enter {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.oj-starter.enter {
			animation: none;
		}
	}
	:global(html[data-reduce-motion]) .oj-starter.enter {
		animation: none;
	}
</style>
