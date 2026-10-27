<script lang="ts">
	import { CAMELOT_RULES, type CamelotRuleId } from '$lib/player/key/camelot-rules.generated';
	import { patchCompatibleFilter, uiPrefs } from '$lib/rb/prefs.svelte';

	// Loaded on first hover by CompatibleFilterPopover, so the range buttons stay
	// out of the /performance route's eager bundle.
	let { onEnter, onLeave }: { onEnter: () => void; onLeave: () => void } = $props();

	// A range change commits only after the disk write lands (PR #4014, Sol P1);
	// a rejection is shown here instead of leaving a range the disk never took.
	let saveError = $state<string | null>(null);

	function _patch(patch: Parameters<typeof patchCompatibleFilter>[0]): void {
		saveError = null;
		patchCompatibleFilter(patch).catch((err: unknown) => {
			const detail = err instanceof Error ? err.message : String(err);
			saveError = `Could not save compatible filter: ${detail}`;
		});
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

	const ruleSummary = $derived.by(() => {
		const on = CAMELOT_RULES.filter((rule) => shownRules.includes(rule.id));
		return on.length === 0 ? 'key not checked' : on.map((rule) => rule.label).join(', ');
	});

	function _setBpmWindow(n: number): void {
		_patch({ bpm_window_bpm: n, bpm_enabled: true });
	}

	function _setBpmOff(): void {
		_patch({ bpm_enabled: false });
	}

	function _setDirection(d: 'both' | 'above' | 'below' | 'same'): void {
		_patch({ bpm_direction: d });
	}
</script>

<div
	class="compat-filter-panel"
	role="group"
	aria-label="Compatible filter options"
	onmouseenter={onEnter}
	onmouseleave={onLeave}
>
	<p class="compat-filter-about">
		Show only tracks compatible with the reference deck (master, else playing, else any loaded with
		key and BPM): Camelot key family (including half/double BPM folds) and inside the BPM window.
		Shortcut: Tab
	</p>
	<p class="compat-filter-heading">Camelot rules (from the reference key; none ticked = key not checked)</p>
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
	<p class="compat-filter-heading">BPM window</p>
	<div class="compat-filter-row">
		<button type="button" onclick={() => _setBpmWindow(10)}>±10</button>
		<button type="button" onclick={() => _setBpmWindow(20)}>±20</button>
		<button type="button" onclick={_setBpmOff}>off</button>
	</div>
	<p class="compat-filter-heading">BPM direction</p>
	<div class="compat-filter-row">
		<button type="button" onclick={() => _setDirection('both')}>±</button>
		<button type="button" onclick={() => _setDirection('above')}>above</button>
		<button type="button" onclick={() => _setDirection('below')}>below</button>
		<button type="button" onclick={() => _setDirection('same')}>same</button>
	</div>
	<p class="compat-filter-summary" title="Current compatible filter settings">
		Camelot: {ruleSummary};
		{uiPrefs.compatible_filter.bpm_enabled
			? `BPM ±${uiPrefs.compatible_filter.bpm_window_bpm} (${uiPrefs.compatible_filter.bpm_direction})`
			: 'BPM off'}
	</p>
	{#if saveError}
		<p class="compat-filter-error" role="alert" title={saveError}>{saveError}</p>
	{/if}
</div>

<style>
	.compat-filter-panel {
		position: absolute;
		top: 100%;
		left: 0;
		z-index: 50;
		min-width: 12rem;
		padding: 0.5rem;
		background: var(--rb-panel-bg, #1a1a1a);
		border: 1px solid var(--rb-border, #444);
		border-radius: 6px;
	}
	.compat-filter-about {
		margin: 0 0 0.35rem;
		max-width: 22rem;
		font-size: 0.7rem;
		line-height: 1.3;
	}
	.compat-filter-heading {
		margin: 0.25rem 0 0.15rem;
		font-size: 0.7rem;
		opacity: 0.8;
	}
	.compat-filter-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
	}
	.compat-filter-row button {
		font-size: 0.7rem;
		padding: 0.15rem 0.35rem;
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
	.compat-filter-error {
		margin: 0.35rem 0 0;
		font-size: 0.65rem;
		color: var(--rb-danger, #f66);
	}
	.compat-filter-summary {
		margin: 0.35rem 0 0;
		font-size: 0.65rem;
	}
</style>
