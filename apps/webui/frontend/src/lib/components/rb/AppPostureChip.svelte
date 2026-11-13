<script lang="ts">
	import { onMount } from 'svelte';
	import { readMachinePressure } from '$lib/rb/machine-pressure';
	import { setAppPosture, uiPrefs } from '$lib/rb/prefs.svelte';
	import ControlExplainer from './deck/ControlExplainer.svelte';
	import { TOOLTIP_SKIM_ROW } from '$lib/ui/tooltip-timing';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import {
		RECORD_CONTROL_EXPLAINER_DISMISS,
		recordControlExplainerBullets,
		recordControlIdleTitle,
		recordIdleClickOpensPicker
	} from '$lib/sets/performance-recorder';
	import { usePerformanceRecorderChrome } from '$lib/sets/performance-recorder-chrome.svelte';
	import { setRecordingEnabled } from '$lib/sets/recording-flag';
	import { pushToast } from '$lib/stores.svelte';
	import { topbarSelectorNode } from './topbar-selector-node';

	let {
		showRecordControl = false
	}: {
		/** When true and recording is allowed, show REC beside Prep|Gig (pin 894af5672c3b). */
		showRecordControl?: boolean;
	} = $props();

	const helperActive = $derived(uiPrefs.gig_helper === 'on' && uiPrefs.app_posture === 'gig');
	const pressureBand = $derived(readMachinePressure()?.band ?? 'unknown');
	const recordable = $derived(
		showRecordControl && !libraryWorkspace.active && setRecordingEnabled()
	);
	const chrome = usePerformanceRecorderChrome(() => recordable);
	const recordBullets = recordControlExplainerBullets();
	const recordTitle = $derived(
		chrome.rail.tip ??
			(chrome.rail.recording
				? 'Stop set recording'
				: chrome.rail.waiting
					? 'Waiting for audio input'
					: recordControlIdleTitle())
	);

	onMount(() => {
		if (recordable) chrome.mount();
	});

	async function handleRecordClick(): Promise<void> {
		if (!recordable || chrome.recorderBusy) return;
		if (chrome.recorder.active) {
			await chrome.openRecordPanel();
			return;
		}
		if (recordIdleClickOpensPicker(chrome.recorder.active)) {
			await chrome.openRecordPanel();
		}
	}
</script>

<div class="posture-wrap">
	<div
		class="posture-chip"
		role="group"
		aria-label="Resource posture"
		use:topbarSelectorNode={{ name: 'posture', disabled: false }}
	>
		<button
			type="button"
			class="posture-btn"
			aria-pressed={uiPrefs.app_posture === 'prep'}
			title="Resource posture: Prep. Background poll, workers, and prefetch stay at practice levels. Switch to Gig to cap them for a live set. This is not the app-mode chooser."
			onclick={() => setAppPosture('prep')}
		>
			Prep
		</button>
		<button
			type="button"
			class="posture-btn"
			aria-pressed={uiPrefs.app_posture === 'gig'}
			title="Resource posture: Gig. Library poll is 5 minutes, background workers are halved, prefetch is 2 tracks / 24 MiB. This is not the app-mode chooser."
			onclick={() => setAppPosture('gig')}
		>
			Gig
		</button>
	</div>
	{#if recordable}
		<ControlExplainer
			title="Set recording"
			bullets={recordBullets}
			dismiss={RECORD_CONTROL_EXPLAINER_DISMISS}
			pinOnClick={true}
			placement="below"
			tooltip={TOOLTIP_SKIM_ROW}
			action={recordPickerAction}
		>
			<button
				type="button"
				class="record-btn"
				class:recording={chrome.rail.recording}
				class:waiting={chrome.rail.waiting}
				data-testid="topbar-record-control"
				aria-pressed={chrome.rail.recording}
				disabled={chrome.recorderBusy}
				use:topbarSelectorNode={{ name: 'recorder', disabled: chrome.recorderBusy }}
				title={recordTitle}
				onclick={() => void handleRecordClick()}
			>
				REC
			</button>
		</ControlExplainer>
	{/if}
	{#if helperActive}
		<span
			class="helper-on"
			data-testid="gig-helper-on"
			title={`Gig helper is on: system pressure is monitored (${pressureBand} in PerfMeters) and Gig background caps stay active.`}
			aria-label="Gig helper active"
		>
			helper
		</span>
	{/if}
</div>

{#snippet recordPickerAction()}
	{#if chrome.RecordInputPicker !== null}
		<chrome.RecordInputPicker
			variant={chrome.pickerVariant}
			onstarted={(status) => chrome.onPickerStarted(status)}
			oncancel={() => chrome.closePicker()}
			notify={pushToast}
		/>
	{/if}
{/snippet}

<style>
	.posture-wrap {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		margin-left: 6px;
	}
	.posture-chip {
		display: inline-flex;
		gap: 2px;
	}
	.posture-btn {
		font: inherit;
		font-size: 10px;
		line-height: 1;
		padding: 3px 6px;
		border: 1px solid var(--rb-border, #444);
		background: transparent;
		color: var(--rb-text-muted, #aaa);
		cursor: pointer;
	}
	.posture-btn[aria-pressed='true'] {
		border-color: var(--rb-accent, #4af);
		color: var(--rb-text, #fff);
	}
	.record-btn {
		font: inherit;
		font-size: 10px;
		font-weight: 700;
		line-height: 1;
		padding: 3px 7px;
		border: 1px solid var(--rb-border, #444);
		background: #2a1515;
		color: #e8a8a4;
		cursor: pointer;
		letter-spacing: 0.04em;
	}
	.record-btn.recording {
		background: #4a1717;
		border-color: #d0342c;
		color: #fff;
		box-shadow: inset 0 0 0 1px #d0342c;
	}
	.record-btn.waiting {
		border-color: #d09a2c;
		animation: rec-waiting 1.2s ease-in-out infinite;
	}
	.record-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
	@keyframes rec-waiting {
		50% {
			border-color: transparent;
		}
	}
	:global(html[data-reduce-motion]) .record-btn.waiting {
		animation: none;
	}
	@media (prefers-reduced-motion: reduce) {
		.record-btn.waiting {
			animation: none;
		}
	}
	.helper-on {
		font-size: 9px;
		font-weight: 700;
		color: var(--rb-accent, #4af);
		text-transform: lowercase;
		cursor: help;
	}
</style>
