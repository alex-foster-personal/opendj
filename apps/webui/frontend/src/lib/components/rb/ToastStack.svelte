<!--
	The app-wide toast tray (UX-TOAST-01 + UX-TOAST-02, issue #3882).

	Compact headlines, visible click-to-copy hints, expandable detail, gesture
	dismiss, and telemetry ids in the clipboard payload.
-->
<script lang="ts">
	import { TOAST_SOLUTION_URL_PLACEHOLDER } from '$lib/toast-presentation';
	import {
		copyToast,
		dismissToast,
		finalizeToastExit,
		holdToast,
		releaseToast,
		toggleToastExpanded,
		type Toast
	} from '$lib/stores.svelte';
	import { TOAST_EXIT_DURATION_MS, TOAST_EXIT_TRANSLATE_PX } from '$lib/toast-tray-policy';

	let { items }: { items: readonly Toast[] } = $props();

	$effect(() => {
		const handles: ReturnType<typeof setTimeout>[] = [];
		for (const toast of items) {
			if (toast.exiting !== true) continue;
			handles.push(
				setTimeout(() => finalizeToastExit(toast.logId), TOAST_EXIT_DURATION_MS + 40)
			);
		}
		return () => {
			for (const handle of handles) clearTimeout(handle);
		};
	});

	let copyState = $state<Record<string, 'copied' | string>>({});
	let touchStartX = $state<number | null>(null);

	async function copy(toast: Toast): Promise<void> {
		try {
			await copyToast(toast.logId);
			copyState[toast.logId] = 'copied';
			setTimeout(() => {
				delete copyState[toast.logId];
			}, 1500);
		} catch (exc) {
			copyState[toast.logId] = exc instanceof Error ? exc.message : String(exc);
		}
	}

	function hasExpandableDetail(toast: Toast): boolean {
		if (toast.detail !== undefined && toast.detail !== toast.headline) return true;
		return toast.kind === 'error' && toast.solutionHint !== undefined;
	}

	function onWheel(event: WheelEvent, logId: string): void {
		if (event.deltaY === 0 && event.deltaX === 0) return;
		event.preventDefault();
		dismissToast(logId);
	}

	function onTouchStart(event: TouchEvent): void {
		if (event.touches.length !== 1) return;
		touchStartX = event.touches[0].clientX;
	}

	function onTouchMove(event: TouchEvent, logId: string): void {
		if (touchStartX === null || event.touches.length !== 1) return;
		const deltaX = event.touches[0].clientX - touchStartX;
		if (Math.abs(deltaX) > 40) {
			touchStartX = null;
			dismissToast(logId);
		}
	}

	function onTouchEnd(): void {
		touchStartX = null;
	}

	function onExitTransitionEnd(event: TransitionEvent, toast: Toast): void {
		if (toast.exiting !== true) return;
		if (event.propertyName !== 'transform' && event.propertyName !== 'opacity') return;
		finalizeToastExit(toast.logId);
	}
</script>

<div class="toast-stack">
	{#each items as toast (toast.id)}
		<div
			class="toast"
			class:warn={toast.kind === 'warn'}
			class:error={toast.kind === 'error'}
			class:toast-exiting={toast.exiting === true}
			role="status"
			data-toast-id={toast.logId}
			data-toast-exiting={toast.exiting === true ? toast.logId : undefined}
			style={`--toast-exit-ms: ${TOAST_EXIT_DURATION_MS}ms; --toast-exit-px: ${TOAST_EXIT_TRANSLATE_PX}px`}
			ontransitionend={(event) => onExitTransitionEnd(event, toast)}
			onmouseenter={() => holdToast(toast.logId)}
			onmouseleave={() => releaseToast(toast.logId)}
			onwheel={(event) => onWheel(event, toast.logId)}
			ontouchstart={onTouchStart}
			ontouchmove={(event) => onTouchMove(event, toast.logId)}
			ontouchend={onTouchEnd}
		>
			<button
				type="button"
				class="toast-body"
				data-toast-copy={toast.logId}
				title="Click to copy this message with its id, telemetry ids, timestamp and environment."
				onclick={() => copy(toast)}
			>
				<span class="toast-headline">{toast.headline}</span>
				<span class="toast-copy-hint" data-toast-copy-hint={toast.logId}>Click to copy</span>
				{#if hasExpandableDetail(toast) && toast.expanded === true}
					{#if toast.detail !== undefined && toast.detail !== toast.headline}
						<span class="toast-detail">{toast.detail}</span>
					{/if}
					{#if toast.kind === 'error' && toast.solutionHint !== undefined}
						<span class="toast-solution">
							{toast.solutionHint.replace(TOAST_SOLUTION_URL_PLACEHOLDER, '')}
							<a
								href={TOAST_SOLUTION_URL_PLACEHOLDER}
								target="_blank"
								rel="noopener noreferrer"
								onclick={(event) => event.stopPropagation()}>read more (TBD)</a
							>
						</span>
					{/if}
				{/if}
				{#if toast.count > 1}
					<span class="toast-note" data-toast-count={toast.count}>Repeated {toast.count} times</span>
				{/if}
				{#if copyState[toast.logId] === 'copied'}
					<span class="toast-note" data-toast-copied={toast.logId}>copied</span>
				{:else if copyState[toast.logId] !== undefined}
					<span class="toast-note failed" data-toast-copy-failed={toast.logId}
						>{copyState[toast.logId]}</span
					>
				{/if}
				{#if toast.action !== undefined}
					<span class="toast-action-row">
						<button
							type="button"
							class="toast-action"
							data-toast-action={toast.logId}
							onclick={(event) => {
								event.stopPropagation();
								toast.action?.handler();
							}}>{toast.action.label}</button
						>
					</span>
				{/if}
			</button>
			{#if hasExpandableDetail(toast)}
				<button
					type="button"
					class="toast-expand"
					data-toast-expand={toast.logId}
					aria-expanded={toast.expanded === true}
					title={toast.expanded === true ? 'Hide details' : 'Show details'}
					onclick={(event) => {
						event.stopPropagation();
						toggleToastExpanded(toast.logId);
					}}>{toast.expanded === true ? '▾' : '▸'}</button
				>
			{/if}
			<button
				type="button"
				class="toast-dismiss"
				data-toast-dismiss={toast.logId}
				title="Dismiss this message"
				aria-label={`Dismiss message ${toast.logId}`}
				onclick={(event) => {
					event.stopPropagation();
					dismissToast(toast.logId);
				}}>x</button
			>
		</div>
	{/each}
</div>

<style>
	.toast {
		display: flex;
		align-items: flex-start;
		gap: 0.35rem;
	}
	.toast-body {
		flex: 1 1 auto;
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		padding: 0;
		cursor: pointer;
	}
	.toast-body:hover .toast-headline {
		text-decoration: underline dotted;
	}
	.toast-headline {
		font-size: 0.92em;
		line-height: 1.25;
	}
	.toast-copy-hint {
		font-size: 0.72em;
		opacity: 0.75;
	}
	.toast-detail {
		font-size: 0.78em;
		opacity: 0.9;
		white-space: pre-wrap;
	}
	.toast-solution {
		font-size: 0.78em;
		opacity: 0.85;
	}
	.toast-solution a {
		color: inherit;
		text-decoration: underline;
	}
	.toast-note {
		font-size: 0.75em;
		opacity: 0.8;
	}
	.toast-note.failed {
		color: var(--danger);
		opacity: 1;
	}
	.toast-expand {
		flex: 0 0 auto;
		border: 1px solid var(--border);
		border-radius: 3px;
		background: transparent;
		color: inherit;
		font: inherit;
		line-height: 1.1;
		padding: 0 0.25rem;
		cursor: pointer;
	}
	.toast-dismiss {
		flex: 0 0 auto;
		border: 1px solid var(--border);
		border-radius: 3px;
		background: transparent;
		color: inherit;
		font: inherit;
		line-height: 1.1;
		padding: 0 0.3rem;
		cursor: pointer;
	}
	.toast-dismiss:hover {
		background: var(--danger);
		color: #fff;
	}
	.toast-action-row {
		margin-top: 0.25rem;
	}
	.toast-action {
		border: 1px solid var(--border);
		border-radius: 3px;
		background: transparent;
		color: inherit;
		font: inherit;
		padding: 0.1rem 0.45rem;
		cursor: pointer;
	}
	.toast-action:hover {
		background: var(--accent);
		color: #fff;
	}
	.toast-exiting {
		transform: translateY(calc(-1 * var(--toast-exit-px, 100px)));
		opacity: 0;
		transition:
			transform var(--toast-exit-ms, 100ms) ease,
			opacity var(--toast-exit-ms, 100ms) ease;
		pointer-events: none;
	}
</style>
