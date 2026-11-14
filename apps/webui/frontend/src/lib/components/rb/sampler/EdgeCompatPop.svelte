<script lang="ts">
	/**
	 * "Compatible with master" popover of the top tab's caret (spec 7.4, mock `openCompatPop`):
	 * Mode, Camelot rules, BPM window with half / double, BPM direction and key shift, then the
	 * summary line. It edits the library's own compatible prefs (one shared object, spec 7.4), so
	 * a change here also moves the library filter. Placed beside the top tab like the set name
	 * dialog (476 + 10 px from the edge, 8 px down). A press anywhere else or Esc closes it.
	 */
	import { CAMELOT_RULES, type CamelotRuleId } from '$lib/player/key/camelot-rules.generated';
	import { HEAD_OPEN_W } from '$lib/player/sampler/edge-wave';
	import type { Edge } from '$lib/player/sampler/sampler-types';
	import { compatibleFilterSummary } from '$lib/rb/compatible-filter-copy';
	import type { CompatibleFilterPrefs } from '$lib/rb/compatible-filter-prefs';
	import { patchCompatibleFilter, uiPrefs } from '$lib/rb/prefs.svelte';

	let { edge, onclose }: { edge: Edge; onclose: () => void } = $props();

	const MODES = [
		['transition', 'Transition (T)'],
		['mashup', 'Mashup (M)']
	] as const;
	const DIRECTIONS = [
		['both', 'Both'],
		['above', 'Above'],
		['below', 'Below'],
		['same', 'Same']
	] as const;
	const SHIFTS = [0, 1, 2, 3] as const;
	const side = `${HEAD_OPEN_W + 10}px`;

	const prefs = $derived(uiPrefs.compatible_filter);
	let error = $state('');
	// Rules sent but not yet committed, so two quick ticks both survive (same as the library panel).
	let pendingRules = $state<readonly CamelotRuleId[] | null>(null);
	let rulesRequest = 0;
	const shownRules = $derived(pendingRules ?? prefs.rules);

	function patch(p: Partial<CompatibleFilterPrefs>): Promise<void> {
		error = '';
		return patchCompatibleFilter(p).catch((err: unknown) => {
			error = `Not saved: ${err instanceof Error ? err.message : String(err)}`;
		});
	}

	function toggleRule(id: CamelotRuleId, on: boolean): void {
		const next = CAMELOT_RULES.map((r) => r.id).filter((r) =>
			r === id ? on : shownRules.includes(r)
		);
		pendingRules = next;
		const request = ++rulesRequest;
		void patch({ rules: next }).finally(() => {
			if (request === rulesRequest) pendingRules = null;
		});
	}

	function setWindow(raw: string): void {
		const v = Math.round(Number(raw));
		if (raw !== '' && Number.isFinite(v) && v >= 0 && v <= 60) void patch({ bpm_window_bpm: v });
	}

	function onKey(ev: KeyboardEvent): void {
		const key = ev.key.toLowerCase();
		if (ev.key === 'Escape') onclose();
		else if (
			(key === 't' || key === 'm') &&
			!(ev.target instanceof HTMLInputElement && ev.target.type === 'number')
		)
			void patch({ mode: key === 't' ? 'transition' : 'mashup' });
	}
</script>

<svelte:window onpointerdown={onclose} />
<div
	class="pop"
	role="dialog"
	tabindex="-1"
	aria-label="Compatible with master"
	data-testid="edge-compat-pop-{edge}"
	style:top="8px"
	style:right={edge === 'R' ? side : undefined}
	style:left={edge === 'L' ? side : undefined}
	onpointerdown={(ev) => ev.stopPropagation()}
	oncontextmenu={(ev) => ev.stopPropagation()}
	onkeydown={onKey}
>
	<h3>Compatible with master</h3>
	<div>
		<div class="lbl">Mode</div>
		<div class="seg">
			{#each MODES as [v, t] (v)}
				<button
					type="button"
					aria-pressed={prefs.mode === v}
					onclick={() => void patch({ mode: v })}>{t}</button
				>
			{/each}
		</div>
	</div>
	<div class="lbl">Camelot rules</div>
	<div class="rules">
		{#each CAMELOT_RULES as r (r.id)}
			<label class="ck" title={r.example}
				><input
					type="checkbox"
					checked={shownRules.includes(r.id)}
					onchange={(ev) => toggleRule(r.id, ev.currentTarget.checked)}
				/>{r.label}</label
			>
		{/each}
	</div>
	<div class="row">
		<label class="ck"
			><input
				type="checkbox"
				checked={prefs.bpm_enabled}
				onchange={(ev) => void patch({ bpm_enabled: ev.currentTarget.checked })}
			/> BPM within +-</label
		>
		<input
			type="number"
			min="0"
			max="60"
			step="1"
			aria-label="BPM window"
			value={prefs.bpm_window_bpm}
			oninput={(ev) => setWindow(ev.currentTarget.value)}
		/>
		<label class="ck"
			><input
				type="checkbox"
				checked={prefs.allow_half_double}
				onchange={(ev) => void patch({ allow_half_double: ev.currentTarget.checked })}
			/> half / double</label
		>
	</div>
	<div>
		<div class="lbl">BPM direction</div>
		<div class="seg">
			{#each DIRECTIONS as [v, t] (v)}
				<button
					type="button"
					aria-pressed={prefs.bpm_direction === v}
					onclick={() => void patch({ bpm_direction: v })}>{t}</button
				>
			{/each}
		</div>
	</div>
	<div>
		<div class="lbl">Key shift allowed (samples play shifted)</div>
		<div class="seg">
			{#each SHIFTS as n (n)}
				<button
					type="button"
					aria-pressed={prefs.key_shift_max === n}
					onclick={() => void patch({ key_shift_max: n })}>{n}</button
				>
			{/each}
		</div>
	</div>
	<div class="sum">{compatibleFilterSummary(prefs)}</div>
	{#if error}<div class="sum err" role="alert">{error}</div>{/if}
</div>

<style>
	/* Mock `.pop`, `.seg`, `.rules`, `.ck`, `.row`, `.sum`. */
	.pop {
		position: absolute;
		z-index: 9;
		pointer-events: auto;
		box-sizing: border-box;
		width: 330px;
		display: grid;
		gap: 9px;
		padding: 12px 14px;
		background: var(--es-panel);
		border: 1px solid var(--es-border-strong);
		border-radius: 7px;
		box-shadow: 0 14px 44px var(--es-shadow-pop);
		font: 13px var(--es-font-body);
		color: var(--es-fg);
		white-space: normal;
		text-align: left;
	}
	h3 {
		margin: 0;
		font: 18px var(--es-font-display);
		letter-spacing: 0.5px;
		text-transform: uppercase;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.lbl {
		font: 13px var(--es-font-display);
		text-transform: uppercase;
		letter-spacing: 0.6px;
		color: var(--es-muted);
	}
	.seg {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;
	}
	.seg button {
		font: 13px var(--es-font-body);
		background: var(--es-raised);
		color: var(--es-fg);
		border: 1px solid var(--es-line);
		border-radius: 4px;
		padding: 3px 9px;
		cursor: pointer;
	}
	.seg button[aria-pressed='true'] {
		background: var(--es-accent);
		color: var(--es-accent-ink);
		border-color: var(--es-accent);
	}
	.rules {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 4px 10px;
	}
	.ck {
		display: flex;
		gap: 6px;
		align-items: center;
		color: var(--es-fg);
	}
	input[type='checkbox'] {
		accent-color: var(--es-accent);
		margin: 0;
	}
	input[type='number'] {
		width: 62px;
		box-sizing: border-box;
		background: var(--es-input-bg);
		color: var(--es-fg);
		border: 1px solid var(--es-line);
		border-radius: 4px;
		padding: 4px 7px;
		font: 13px var(--es-font-body);
	}
	.row {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}
	.sum {
		font: 12px var(--es-font-mono);
		color: var(--es-muted);
	}
	.sum.err {
		color: var(--es-bad);
	}
</style>
