<script lang="ts">
	import { onMount } from 'svelte';
	import { CAMELOT_RULES, type CamelotRuleId } from '$lib/player/key/camelot-rules.generated';
	import { patchCompatibleFilter, uiPrefs } from '$lib/rb/prefs.svelte';
	import { openSettings } from '$lib/settings/overlay.svelte';
	import {
		compatibleModeDefinition,
		compatibleFilterSummary
	} from '$lib/rb/compatible-filter-copy';
	import type {
		CompatibleBpmDirection,
		CompatibleKeyShiftMax,
		CompatibleMode
	} from '$lib/rb/compatible-filter-prefs';
	import { compatPanelPlacement, placementStyle } from './compat-panel-placement';

	// Loaded on first hover by CompatibleFilterPopover, so the range buttons stay
	// out of the /performance route's eager bundle.
	let { onEnter, onLeave }: { onEnter: () => void; onLeave: () => void } = $props();

	// A range change commits only after the disk write lands (PR #4014, Sol P1);
	// a rejection is shown here instead of leaving a range the disk never took.
	let saveError = $state<string | null>(null);

	const prefs = $derived(uiPrefs.compatible_filter);

	// The library toolbar usually sits near the bottom of the window, and the
	// library pane clips its overflow, so the panel is fixed-positioned against
	// the checkbox, opens upward when there is no room below, and is capped to
	// the room it has (it scrolls). Re-placed whenever its content resizes
	// (Mashup to Transition adds the rule list) and when the window resizes.
	let panelEl = $state<HTMLDivElement | null>(null);
	let placement = $state('visibility:hidden');
	function _place(): void {
		const anchor = panelEl?.parentElement;
		if (panelEl === null || panelEl === undefined || anchor === null || anchor === undefined) return;
		// scrollHeight is the natural height, unaffected by the max-height cap.
		const next = placementStyle(
			compatPanelPlacement(anchor.getBoundingClientRect(), panelEl.scrollHeight, panelEl.offsetWidth, {
				width: window.innerWidth,
				height: window.innerHeight
			})
		);
		if (next !== placement) placement = next;
	}
	onMount(() => {
		_place();
		if (panelEl === null) return;
		const observer = new ResizeObserver(_place);
		for (const child of Array.from(panelEl.children)) observer.observe(child);
		observer.observe(panelEl);
		window.addEventListener('resize', _place);
		return () => {
			observer.disconnect();
			window.removeEventListener('resize', _place);
		};
	});

	function _patch(patch: Parameters<typeof patchCompatibleFilter>[0]): void {
		saveError = null;
		patchCompatibleFilter(patch).catch((err: unknown) => {
			const detail = err instanceof Error ? err.message : String(err);
			saveError = `Could not save compatible filter: ${detail}`;
		});
	}

	function _setMode(mode: CompatibleMode): void {
		_patch({ mode });
	}

	function _setShift(n: CompatibleKeyShiftMax): void {
		_patch({ key_shift_max: n });
	}

	// The rules list sent but not yet committed (uiPrefs changes only once the PUT
	// lands). A second tick before then builds on this, not on the stale committed
	// list, so two quick toggles both survive (PR #7557, Sol P1).
	// Cleared by a scalar request number, not by array identity: $state proxies arrays.
	let pendingRules = $state<readonly CamelotRuleId[] | null>(null);
	let rulesRequest = 0;
	const shownRules = $derived<readonly CamelotRuleId[]>(pendingRules ?? uiPrefs.compatible_filter.rules);

	/** Tick or untick one rule; the stored list keeps the table's order. */
	function _toggleRule(id: CamelotRuleId, on: boolean): void {
		const enabled = new Set(shownRules);
		if (on) enabled.add(id);
		else enabled.delete(id);
		const next = CAMELOT_RULES.map((rule) => rule.id).filter((rid) => enabled.has(rid));
		pendingRules = next;
		const request = ++rulesRequest;
		saveError = null;
		patchCompatibleFilter({ rules: next })
			.catch((err: unknown) => {
				// Only the newest list matters: it replaces any earlier one in full.
				if (request !== rulesRequest) return;
				const detail = err instanceof Error ? err.message : String(err);
				saveError = `Could not save compatible filter: ${detail}`;
			})
			.finally(() => {
				if (request === rulesRequest) pendingRules = null;
			});
	}

	function _setBpmWindow(n: number): void {
		_patch({ bpm_window_bpm: n, bpm_enabled: true });
	}

	function _setBpmOff(): void {
		_patch({ bpm_enabled: false });
	}

	function _setDirection(d: CompatibleBpmDirection): void {
		_patch({ bpm_direction: d });
	}

	function _openModeSettings(): void {
		openSettings('compatible');
	}

	/** T / M switch modes while the dropdown has focus; A/D and W/S move like
	 * the arrow keys (WASD rule). A text field never reaches here: the panel
	 * holds buttons and rule checkboxes only. The popover calls this for keys pressed on its checkbox. */
	export function handleKey(e: KeyboardEvent): void {
		if (e.metaKey || e.ctrlKey || e.altKey) return;
		const key = e.key.toLowerCase();
		if (key === 't' || key === 'm') {
			e.preventDefault();
			_setMode(key === 't' ? 'transition' : 'mashup');
			return;
		}
		const step = { arrowleft: -1, a: -1, arrowup: -1, w: -1, arrowright: 1, d: 1, arrowdown: 1, s: 1 }[key];
		if (step === undefined || panelEl === null) return;
		const buttons = Array.from(panelEl.querySelectorAll<HTMLElement>('button, input[type="checkbox"]'));
		const at = buttons.indexOf(document.activeElement as HTMLElement);
		const next = buttons[(at + step + buttons.length) % buttons.length];
		if (next === undefined) return;
		e.preventDefault();
		next.focus();
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div
	bind:this={panelEl}
	class="compat-filter-panel"
	data-owns-letter-keys=""
	style={placement}
	role="group"
	aria-label="Compatible filter options"
	onmouseenter={onEnter}
	onmouseleave={onLeave}
>
	<p class="compat-filter-about">
		Show only tracks compatible with the reference deck (master, else playing, else any loaded with
		key and BPM): the mode's key rule and the BPM window (including half/double BPM folds). Shortcut:
		Tab
	</p>
	<p class="compat-filter-heading">Mode</p>
	<div class="compat-filter-toggle">
		<button
			type="button"
			aria-pressed={prefs.mode === 'transition'}
			title="Transition compatible (T)"
			onclick={() => _setMode('transition')}>Transition</button
		>
		<button
			type="button"
			aria-pressed={prefs.mode === 'mashup'}
			title="Mashup compatible (M)"
			onclick={() => _setMode('mashup')}>Mashup</button
		>
	</div>
	<p class="compat-filter-about">{compatibleModeDefinition(prefs)}</p>
	{#if prefs.mode === 'transition'}
	<p class="compat-filter-heading">Transition: Camelot rules (from the reference key; none ticked = key not checked)</p>
	<div class="compat-filter-rules" role="group" aria-label="Camelot rules">
		{#each CAMELOT_RULES as rule (rule.id)}
			<label class="compat-filter-rule" title={rule.example} data-testid={`compat-rule-${rule.id}`}>
				<input
					type="checkbox"
					checked={shownRules.includes(rule.id)}
					onchange={(e) => _toggleRule(rule.id, e.currentTarget.checked)}
				/>
				<span>{rule.label}</span>
				<span class="compat-filter-rule-example">{rule.example}</span>
			</label>
		{/each}
	</div>
	{/if}
	<p class="compat-filter-heading">Key shift</p>
	<div class="compat-filter-row">
		{#each [0, 1, 2, 3] as const as n (n)}
			<button
				type="button"
				aria-pressed={prefs.key_shift_max === n}
				title={n === 0 ? 'No key shift' : `Match tracks up to ${n} semitone${n === 1 ? '' : 's'} out; they load shifted`}
				onclick={() => _setShift(n)}>{n === 0 ? 'off' : `±${n}`}</button
			>
		{/each}
	</div>
	<p class="compat-filter-heading">BPM window</p>
	<div class="compat-filter-row">
		<button type="button" aria-pressed={prefs.bpm_enabled && prefs.bpm_window_bpm === 10} onclick={() => _setBpmWindow(10)}>±10</button>
		<button type="button" aria-pressed={prefs.bpm_enabled && prefs.bpm_window_bpm === 20} onclick={() => _setBpmWindow(20)}>±20</button>
		<button type="button" aria-pressed={!prefs.bpm_enabled} onclick={_setBpmOff}>off</button>
	</div>
	<p class="compat-filter-heading">BPM direction</p>
	<div class="compat-filter-row">
		<button type="button" aria-pressed={prefs.bpm_direction === 'both'} onclick={() => _setDirection('both')}>±</button>
		<button type="button" aria-pressed={prefs.bpm_direction === 'above'} onclick={() => _setDirection('above')}>above</button>
		<button type="button" aria-pressed={prefs.bpm_direction === 'below'} onclick={() => _setDirection('below')}>below</button>
		<button type="button" aria-pressed={prefs.bpm_direction === 'same'} onclick={() => _setDirection('same')}>same</button>
	</div>
	<div class="compat-filter-summary">
		<span title="Current compatible filter settings">{compatibleFilterSummary(prefs)}</span>
		<button type="button" class="compat-filter-link" onclick={_openModeSettings}>Edit modes in Settings</button>
	</div>
	{#if saveError}
		<p class="compat-filter-error" role="alert" title={saveError}>{saveError}</p>
	{/if}
</div>

<style>
	.compat-filter-panel {
		position: fixed;
		overflow-y: auto;
		overscroll-behavior: contain;
		z-index: 50;
		box-sizing: border-box;
		width: 300px;
		max-width: calc(100vw - 48px);
		padding: 10px;
		display: grid;
		gap: 8px;
		color: var(--rb-text, #c8cdd2);
		background: var(--rb-panel, #14171d);
		border: 1px solid var(--rb-border, #444);
		border-radius: 6px;
		box-shadow: 0 8px 22px rgba(0, 0, 0, 0.35);
		white-space: normal;
	}
	.compat-filter-heading {
		margin: 0;
		font-family: ui-monospace, Menlo, monospace;
		font-size: 10px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--rb-text-dim, #838990);
	}
	.compat-filter-about {
		margin: 0;
		font-size: 11px;
		line-height: 1.35;
		color: var(--rb-text-dim, #838990);
	}
	.compat-filter-toggle {
		display: grid;
		grid-template-columns: 1fr 1fr;
		border: 1px solid var(--rb-border, #444);
		border-radius: 4px;
		overflow: hidden;
	}
	.compat-filter-toggle button {
		font: inherit;
		font-size: 11.5px;
		padding: 5px 6px;
		border: 0;
		background: var(--rb-panel-raised, #1a1e25);
		color: var(--rb-text-dim, #838990);
		cursor: pointer;
	}
	.compat-filter-toggle button + button {
		border-left: 1px solid var(--rb-border, #444);
	}
	.compat-filter-toggle button[aria-pressed='true'] {
		background: var(--rb-select, #1d3f73);
		color: var(--rb-text, #c8cdd2);
		font-weight: 600;
	}
	.compat-filter-row {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.compat-filter-row button {
		font: inherit;
		font-size: 11px;
		padding: 2px 7px;
		background: var(--rb-panel-raised, #1a1e25);
		color: var(--rb-text, #c8cdd2);
		border: 1px solid var(--rb-border, #444);
		border-radius: 3px;
		cursor: pointer;
	}
	.compat-filter-row button[aria-pressed='true'] {
		border-color: var(--rb-accent, #2f6fd6);
		color: var(--rb-accent, #2f6fd6);
	}
	:global(html[data-skin='mono-dev']) .compat-filter-toggle button[aria-pressed='true'],
	:global(html[data-skin='mono-dev']) .compat-filter-row button[aria-pressed='true'] {
		background: #d6d6d6;
		border-color: #d6d6d6;
		color: #0a0a0a;
	}
	.compat-filter-rules {
		display: grid;
		gap: 0.1rem;
	}
	.compat-filter-rule {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		font-size: 0.7rem;
	}
	.compat-filter-rule-example {
		margin-left: auto;
		opacity: 0.65;
		font-variant-numeric: tabular-nums;
	}
	.compat-filter-summary {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		gap: 8px;
		padding-top: 6px;
		border-top: 1px solid var(--rb-border, #444);
		font-size: 10.5px;
		color: var(--rb-text-dim, #838990);
	}
	.compat-filter-link {
		font: inherit;
		padding: 0;
		border: 0;
		background: none;
		color: var(--rb-accent, #2f6fd6);
		cursor: pointer;
	}
	.compat-filter-error {
		margin: 0;
		font-size: 10.5px;
		color: var(--rb-danger, #f66);
	}
</style>
