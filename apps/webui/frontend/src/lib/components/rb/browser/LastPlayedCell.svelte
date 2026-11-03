<script lang="ts">
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { isUsbTrackId } from '$lib/rb/track-source';
	import {
		displayFacts,
		lastPlayedLabel,
		playHits,
		type PlayFacts,
		type PlaySourceId
	} from './last-played';
	import { loadTrackPlays, peekTrackPlays } from './last-played-load';

	let {
		stableId,
		playCount,
		wire = null
	}: {
		stableId: string;
		playCount: number;
		wire?: PlayFacts | null;
	} = $props();

	let fetched = $state<PlayFacts | null>(null);
	let hovering = $state(false);
	let anchor = $state<HTMLElement | null>(null);

	const facts = $derived(displayFacts({ stableId, playCount, wire, fetched }));
	const view = $derived.by(() => {
		const now = Date.now();
		return { label: lastPlayedLabel(facts, now), hits: playHits(facts, now) };
	});
	const open = $derived(hovering && view.hits.length > 0);
	const aria = $derived(
		view.label === '' && view.hits.length === 0
			? undefined
			: view.label === ''
				? view.hits.map((hit) => `${hit.label}, ${hit.detail}`).join('. ')
				: `Last played ${view.label}. ${view.hits.map((hit) => `${hit.label}, ${hit.detail}`).join('. ')}`
	);

	$effect(() => {
		const id = stableId;
		if (isUsbTrackId(id) || playCount <= 0) {
			fetched = null;
			return;
		}
		const cached = peekTrackPlays(id);
		if (cached !== undefined) {
			fetched = cached;
			return;
		}
		fetched = null;
		let cancelled = false;
		void loadTrackPlays(id).then((next) => {
			if (!cancelled) fetched = next;
		});
		return () => {
			cancelled = true;
		};
	});
</script>

{#snippet sourceIcon(id: PlaySourceId)}
	{#if id === 'rekordbox'}
		<svg class="ico src-rekordbox" viewBox="0 0 14 14" aria-hidden="true"
			><path d="M7 1.1 12.4 7 7 12.9 1.6 7Z" fill="currentColor" /></svg
		>
	{:else if id === 'gig'}
		<svg class="ico src-gig" viewBox="0 0 14 14" aria-hidden="true">
			<circle cx="7" cy="7" r="5.4" fill="none" stroke="currentColor" stroke-width="1.3" />
			<path d="M5.8 4.6v4.8L10 7Z" fill="currentColor" />
		</svg>
	{:else if id === 'rekordbox_usb'}
		<svg class="ico src-rekordbox_usb" viewBox="0 0 14 14" aria-hidden="true">
			<path
				d="M4.6 1.2h1.15V3.4h2.5V1.2H9.4V3.4h.9v3.1a3.3 3.3 0 0 1-6.6 0V3.4h.9Z"
				fill="currentColor"
			/>
			<path d="M6.35 9.6h1.3V12.8h-1.3Z" fill="currentColor" />
		</svg>
	{:else if id === 'serato'}
		<svg class="ico src-serato" viewBox="0 0 14 14" aria-hidden="true">
			<circle cx="7" cy="7" r="5.3" fill="none" stroke="currentColor" stroke-width="1.4" />
			<circle cx="7" cy="7" r="1.35" fill="currentColor" />
		</svg>
	{:else if id === 'djay'}
		<svg class="ico src-djay" viewBox="0 0 14 14" aria-hidden="true">
			<circle cx="3.2" cy="9.2" r="1.5" fill="currentColor" />
			<path d="M4.6 9.2V3.1h1.2l4.2-1.1v1.3L5.8 4.2V9.2" fill="currentColor" />
		</svg>
	{:else}
		<svg class="ico src-traktor" viewBox="0 0 14 14" aria-hidden="true">
			<path d="M2.2 2.4h9.6v1.7H8.15V11.6H5.85V4.1H2.2Z" fill="currentColor" />
		</svg>
	{/if}
{/snippet}

<!-- The cell fills the td so an empty date still hovers. The popover is not
     a pointer target: it stays up while the cursor is on the cell. -->
<span
	class="cell"
	role="group"
	bind:this={anchor}
	data-testid="last-played"
	data-custom-tip=""
	aria-label={aria}
	onpointerenter={() => (hovering = true)}
	onpointerleave={() => (hovering = false)}
>
	{#if view.label !== ''}
		<span class="label">{view.label}</span>
	{:else if view.hits.length > 0}
		<span class="mini">
			{#each view.hits as hit (hit.id)}
				{@render sourceIcon(hit.id)}
			{/each}
		</span>
	{/if}
	{#if open}
		<span
			class="pop"
			role="tooltip"
			data-hover-card=""
			use:triggerFloatingAction={{ getTrigger: () => anchor, preferred: 'above' }}
		>
			<span class="pop-title">Where it played</span>
			{#if view.label !== ''}
				<span class="pop-note" data-testid="last-played-explain"
					>{view.label}: days since the newest play any source below recorded.</span
				>
			{/if}
			{#each view.hits as hit (hit.id)}
				<span class="hit">
					{@render sourceIcon(hit.id)}
					<span class="name">{hit.label}</span>
					<span class="detail">{hit.detail}</span>
				</span>
			{/each}
		</span>
	{/if}
</span>

<style>
	.cell {
		display: flex;
		align-items: center;
		width: 100%;
		height: 100%;
		min-height: 18px;
	}
	.label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
	.mini {
		display: flex;
		align-items: center;
		gap: 2px;
	}
	.pop-note {
		opacity: 0.8;
	}
	.pop {
		position: fixed;
		z-index: 9600;
		display: grid;
		gap: 3px;
		min-width: 196px;
		padding: 6px 8px;
		background: var(--rb-panel, #14171d);
		border: 1px solid var(--rb-border, #2a3038);
		border-radius: 4px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
		color: var(--rb-text, #c8cdd2);
		font-size: 11px;
		pointer-events: none;
	}
	.pop-title {
		font-weight: 600;
		padding-bottom: 3px;
		border-bottom: 1px solid var(--rb-border, #2a3038);
		margin-bottom: 2px;
	}
	.hit {
		display: grid;
		grid-template-columns: 16px 1fr auto;
		gap: 6px;
		align-items: center;
	}
	.name {
		white-space: nowrap;
	}
	.detail {
		color: var(--rb-text-dim, #9aa3ad);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.ico {
		display: block;
		width: 14px;
		height: 14px;
		flex: 0 0 auto;
	}
	.src-rekordbox,
	.src-rekordbox_usb {
		color: #f25c05;
	}
	.src-gig {
		color: #e8a13a;
	}
	.src-serato {
		color: #3dcc6a;
	}
	.src-djay {
		color: #4aa3ff;
	}
	.src-traktor {
		color: #ff5a36;
	}
</style>
