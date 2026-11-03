<script lang="ts">
	// Props-only half of CloudSyncStatusChip: the container polls the status
	// and derives every label through cloudsync-view (which reaches the
	// backend, so this file imports only its types); this file draws the chip. The
	// quick-actions popover is a container, so it arrives as a snippet.
	import type { Snippet } from 'svelte';
	import type { ChipIcon, ChipState } from './cloudsync/cloudsync-view';

	let {
		chipState,
		icon,
		glyphs,
		fullLabel,
		shortLabel,
		title,
		ariaLabel,
		popoverOpen,
		triggerEl = $bindable(),
		togglePopover,
		popover
	}: {
		/** chipState(status), read at each use as the chip always did. */
		chipState: () => ChipState;
		icon: ChipIcon;
		/** chipGlyphs(status): the working state shows a tick instead of words. */
		glyphs: { tick: boolean };
		fullLabel: string;
		shortLabel: string;
		title: string;
		ariaLabel: string;
		popoverOpen: boolean;
		triggerEl?: HTMLButtonElement | null;
		togglePopover: () => void;
		popover: Snippet;
	} = $props();
</script>

<div class="cloudsync-status">
	<button
		bind:this={triggerEl}
		type="button"
		class="chip"
		class:error={chipState() === 'error'}
		class:update-required={chipState() === 'update_required'}
		class:ok={chipState() === 'ok'}
		class:inconclusive={chipState() === 'inconclusive'}
		class:stalled={chipState() === 'off' && icon === 'cloud-alert'}
		title={title}
		aria-label={ariaLabel}
		aria-haspopup="dialog"
		aria-expanded={popoverOpen}
		data-testid="cloudsync-status-chip"
		onclick={togglePopover}
	>
		<!-- CLOUDSYNC-36: never turned on is a neutral slashed cloud; the "!"
		     is only for turned-on-and-failing (chipIcon owns the table). -->
		{#if icon === 'cloud-off'}
			<svg
				class="chip-icon"
				width="12"
				height="12"
				viewBox="0 0 24 24"
				fill="currentColor"
				aria-hidden="true"
				data-testid="cloudsync-chip-icon"
				data-icon="cloud-off"
			>
				<path
					d="M19.35 10.04A7.49 7.49 0 0 0 12 4c-1.48 0-2.85.43-4.01 1.17l1.46 1.46A5.47 5.47 0 0 1 12 6c3.04 0 5.5 2.46 5.5 5.5v.5H19a3 3 0 0 1 1.44 5.62l1.45 1.45A4.99 4.99 0 0 0 24 15c0-2.64-2.05-4.78-4.65-4.96zM3 5.27l2.75 2.74C2.56 8.15 0 10.77 0 14c0 3.31 2.69 6 6 6h11.73l2 2L21 20.73 4.27 4 3 5.27zM7.73 10l8 8H6a4 4 0 0 1 0-8h1.73z"
				/>
			</svg>
		{:else}
			<svg
				class="chip-icon"
				width="12"
				height="12"
				viewBox="0 0 24 24"
				fill="currentColor"
				aria-hidden="true"
				data-testid="cloudsync-chip-icon"
				data-icon={icon}
			>
				<path
					d="M19.35 10.04A7.49 7.49 0 0 0 12 4C9.11 4 6.6 5.64 5.35 8.04A5.994 5.994 0 0 0 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96z"
				/>
			</svg>
		{/if}
		{#if icon === 'cloud-alert'}
			<svg
				class="chip-alert"
				width="4"
				height="10"
				viewBox="0 0 4 12"
				aria-hidden="true"
				data-testid="cloudsync-chip-alert"
			>
				<path d="M2 1.5 V7.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
				<circle cx="2" cy="10.5" r="1.2" fill="currentColor" />
			</svg>
		{/if}
		{#if glyphs.tick}
			<!-- LESSV-03 (the maintainer, Tue 6 Oct 2026): sync "can be just cloud-icon +
			     tick-icon if it's working". The words stay in title and
			     aria-label; every other state keeps its text, because those
			     are the ones that need reading. -->
			<svg
				class="chip-tick"
				width="10"
				height="10"
				viewBox="0 0 12 12"
				aria-hidden="true"
				data-testid="cloudsync-chip-tick"
			>
				<path
					d="M2 6.4 L4.8 9 L10 3"
					fill="none"
					stroke="currentColor"
					stroke-width="1.8"
					stroke-linecap="round"
					stroke-linejoin="round"
				/>
			</svg>
		{:else}
			<span class="chip-label-full">{fullLabel}</span>
			<span class="chip-label-short">{shortLabel}</span>
		{/if}
	</button>
	{#if popoverOpen}
		{@render popover()}
	{/if}
</div>

<style>
	.cloudsync-status {
		position: relative;
		flex: none;
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: 0.2rem;
		box-sizing: border-box;
		max-height: 24px;
		border: 1px solid var(--muted);
		border-radius: 999px;
		background: transparent;
		color: var(--muted);
		font: inherit;
		font-size: 0.72rem;
		line-height: 1;
		padding: 0.1rem 0.45rem;
		white-space: nowrap;
		cursor: pointer;
	}

	/* LESSV-03: sync on reads WHITE, not the orange accent (the maintainer, Tue 6 Oct
	 * 2026). --fg is the shell's own foreground, so it is white on the dark
	 * shells and still readable on the light one. */
	.chip.ok {
		border-color: color-mix(in srgb, var(--fg) 45%, transparent);
		color: var(--fg);
	}

	.chip-tick {
		flex: none;
	}

	.chip.error {
		border-color: var(--danger);
		color: var(--danger);
	}

	.chip.update-required {
		border-color: var(--warning, #b8860b);
		color: var(--warning, #b8860b);
	}

	.chip.inconclusive {
		border-color: var(--warning, #b8860b);
		color: var(--warning, #b8860b);
	}

	/* CLOUDSYNC-36: turned on but not running (no heartbeat, invalid config,
	 * no hub_url) needs action, so it reads amber with the "!", unlike the
	 * muted never-turned-on cloud. */
	.chip.stalled {
		border-color: var(--warning, #b8860b);
		color: var(--warning, #b8860b);
	}

	.chip-icon,
	.chip-alert {
		flex: none;
	}

	@media (max-width: 1024px) {
		.chip-label-full {
			display: none;
		}
	}

	@media (min-width: 1025px) {
		.chip-label-short {
			display: none;
		}
	}
</style>
