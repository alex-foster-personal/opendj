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
		chipGlyphs,
		chipIcon,
		chipTitle
	} from '$lib/components/cloudsync/cloudsync-view';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import { coalesce } from '$lib/rb/coalesce';
	import { publishCloudSyncChipState } from '$lib/rb/cloudsync-chip-state.svelte';
	import CloudSyncStatusChipView from './CloudSyncStatusChipView.svelte';

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
	const glyphs = $derived(chipGlyphs(status));
	const icon = $derived(chipIcon(status));

	async function load(): Promise<void> {
		try {
			status = await getStatus();
			loadError = null;
			publishCloudSyncChipState(status);
		} catch (error: unknown) {
			loadError = error instanceof Error ? error.message : String(error);
			// A failed refresh keeps the last status on screen, so the TopBar
			// clock (CHROME-06) reads that same retained state; publishing null
			// here turned one transient error into 'off' and hid the clock
			// beside a chip still showing syncing/ok (codex review of #3896).
			publishCloudSyncChipState(status);
		}
	}

	// One status read at a time (PERF-RB-04): a poll or a status-changed event that
	// lands while a read is in flight runs once after it, never beside it. On the
	// silver preview each read took over 120 s, so the 30 s interval stacked them.
	const loadOnce = coalesce(load);

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
		// LIBM-138: the first read waits for the boot window to close.
		bootScheduler.defer('cloudsync-chip:load', () => void loadOnce());
		const timer = setInterval(() => {
			if (!document.hidden) void loadOnce();
		}, CHIP_POLL_MS);
		const onStatusChanged = (): void => void loadOnce();
		window.addEventListener(STATUS_CHANGED_EVENT, onStatusChanged);
		return () => {
			clearInterval(timer);
			window.removeEventListener(STATUS_CHANGED_EVENT, onStatusChanged);
		};
	});
</script>

<svelte:window onkeydown={onWindowKeydown} onpointerdown={onWindowPointerDown} />

<CloudSyncStatusChipView
	{chipState}
	{icon}
	{glyphs}
	{fullLabel}
	{shortLabel}
	{title}
	{ariaLabel}
	{popoverOpen}
	bind:triggerEl
	{togglePopover}
>
	{#snippet popover()}
		<CloudSyncQuickActions
			getTrigger={() => triggerEl}
			onclose={closePopover}
			bind:popoverEl
		/>
	{/snippet}
</CloudSyncStatusChipView>
