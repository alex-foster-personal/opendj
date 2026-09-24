<script lang="ts">
	import { onMount } from 'svelte';
	import { getStatus, type CloudSyncStatus } from '$lib/api-cloudsync';
	// The state rules live in one pure module shared with /cloudsync: the chip
	// reads 'off' whenever there is no fresh scheduler heartbeat
	// (status.running false), whatever the config or the last result says.
	import CloudSyncQuickActions from '$lib/components/cloudsync/CloudSyncQuickActions.svelte';
	import {
		CHIP_POLL_MS,
		STATUS_CHANGED_EVENT,
		chipAriaLabel,
		chipFullLabel,
		chipShortLabel,
		chipState as chipStateOf,
		chipTitle
	} from '$lib/components/cloudsync/cloudsync-view';

	let status = $state<CloudSyncStatus | null>(null);
	let loadError = $state<string | null>(null);
	let popoverOpen = $state(false);
	let triggerEl = $state<HTMLButtonElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);

	function chipState(): ReturnType<typeof chipStateOf> {
		return chipStateOf(status);
	}

	const fullLabel = $derived(chipFullLabel(status));
	const shortLabel = $derived(chipShortLabel(status));
	const title = $derived(chipTitle(status, loadError));
	const ariaLabel = $derived(chipAriaLabel(status, loadError));

	async function load(): Promise<void> {
		try {
			status = await getStatus();
			loadError = null;
		} catch (error: unknown) {
			loadError = error instanceof Error ? error.message : String(error);
		}
	}

	function closePopover(): void {
		popoverOpen = false;
		triggerEl?.focus();
	}

	function togglePopover(): void {
		popoverOpen = !popoverOpen;
	}

	function onWindowKeydown(event: KeyboardEvent): void {
		if (!popoverOpen || event.key !== 'Escape') return;
		event.preventDefault();
		closePopover();
	}

	function onWindowPointerDown(event: PointerEvent): void {
		if (!popoverOpen) return;
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (triggerEl?.contains(target)) return;
		if (popoverEl?.contains(target)) return;
		closePopover();
	}

	// Re-read on an interval (a heartbeat that goes stale after load must turn
	// the chip off) and at once when /cloudsync runs Sync now or saves config.
	onMount(() => {
		void load();
		const timer = setInterval(() => {
			if (!document.hidden) void load();
		}, CHIP_POLL_MS);
		const onStatusChanged = (): void => void load();
		window.addEventListener(STATUS_CHANGED_EVENT, onStatusChanged);
		return () => {
			clearInterval(timer);
			window.removeEventListener(STATUS_CHANGED_EVENT, onStatusChanged);
		};
	});
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} />

<div class="cloudsync-status">
	<button
		bind:this={triggerEl}
		type="button"
		class="chip"
		class:error={chipState() === 'error'}
		class:update-required={chipState() === 'update_required'}
		class:ok={chipState() === 'ok'}
		class:inconclusive={chipState() === 'inconclusive'}
		title={title}
		aria-label={ariaLabel}
		aria-haspopup="dialog"
		aria-expanded={popoverOpen}
		data-testid="cloudsync-status-chip"
		onclick={togglePopover}
	>
		<svg
			class="chip-icon"
			width="12"
			height="12"
			viewBox="0 0 24 24"
			fill="currentColor"
			aria-hidden="true"
		>
			<path
				d="M19.35 10.04A7.49 7.49 0 0 0 12 4C9.11 4 6.6 5.64 5.35 8.04A5.994 5.994 0 0 0 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96z"
			/>
		</svg>
		<span class="chip-label-full">{fullLabel}</span>
		<span class="chip-label-short">{shortLabel}</span>
	</button>
	{#if popoverOpen}
		<CloudSyncQuickActions
			getTrigger={() => triggerEl}
			onclose={closePopover}
			bind:popoverEl
		/>
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

	.chip.ok {
		border-color: var(--accent-dim);
		color: var(--accent);
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

	.chip-icon {
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
