<script lang="ts">
	import {
		ANALYSIS_SOURCE_FEATURES,
		type AnalysisSource,
		analysisSourceState
	} from '$lib/rb/analysis-source.svelte';
	import { setShowSections, uiPrefs } from '$lib/rb/prefs.svelte';
	import { sectionsLoadError } from '$lib/components/rb/wave/sections-lazy.svelte';

	let {
		style: menuStyle,
		onPick,
		onShow,
		onClose
	}: {
		style: string;
		onPick: (feature: (typeof ANALYSIS_SOURCE_FEATURES)[number], source: AnalysisSource) => void;
		onShow: () => void;
		onClose: () => void;
	} = $props();

	const FEATURE_LABEL: Record<string, string> = {
		beatgrid: 'Beatgrid',
		key: 'Key',
		waveform: 'Waveform',
		loudness: 'Loudness',
		vocal: 'Vocal',
		phrase: 'Phrase'
	};

	function _featureLabel(feature: string): string {
		return FEATURE_LABEL[feature] ?? feature;
	}

	function _ownDisabled(feature: (typeof ANALYSIS_SOURCE_FEATURES)[number]): boolean {
		return !analysisSourceState.serving.includes(feature);
	}

	function _ownTitle(feature: (typeof ANALYSIS_SOURCE_FEATURES)[number]): string {
		if (!_ownDisabled(feature)) return 'Select own analysis for this lane';
		return `${feature} lane has no serving implementation yet`;
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="src-menu"
	data-hover-card=""
	style={menuStyle}
	role="menu"
	tabindex="-1"
	aria-label="Analysis source"
	onpointerenter={onShow}
	onpointerleave={onClose}
>
	<p class="src-head">Source (testing - resets on relaunch)</p>
	<p class="src-sub">rbx-vs-own A/B: pick which lane each own-rolled feature reads from. On relaunch each lane goes back to its persisted default (own if promoted, rbx otherwise).</p>
	{#each ANALYSIS_SOURCE_FEATURES as feature (feature)}
		{@const current = analysisSourceState.features[feature] ?? 'rekordbox'}
		<div class="src-row">
			<span class="src-label">
				{_featureLabel(feature)}
				{#if feature === 'phrase'}
					<!-- STRUCT-05: persisted show_sections pref (PUT /api/v1/ui-prefs),
					     next to the source choice it visualizes. -->
					<label
						class="src-show"
						title="Show sections: paint each section as a labeled band over the deck and main waveforms"
					>
						<input
							type="checkbox"
							data-testid="show-sections-toggle"
							checked={uiPrefs.show_sections}
							onchange={(e) => setShowSections(e.currentTarget.checked)}
						/>
						show
					</label>
					{#if uiPrefs.show_sections && sectionsLoadError() !== null}
						<!-- A painter that cannot load paints no bands; say so rather
						     than look like a track with no sections. -->
						<span
							class="src-err"
							data-testid="show-sections-error"
							title={`Section overlay failed to load, retrying: ${sectionsLoadError()}`}
						>
							load failed
						</span>
					{/if}
				{/if}
			</span>
			<div class="src-seg" role="group" aria-label={`${feature} source`}>
				<button
					type="button"
					class:active={current === 'rekordbox'}
					onclick={() => onPick(feature, 'rekordbox')}
				>
					RBX
				</button>
				<button
					type="button"
					class:active={current === 'own'}
					disabled={_ownDisabled(feature)}
					title={_ownTitle(feature)}
					onclick={() => onPick(feature, 'own')}
				>
					OWN
				</button>
			</div>
		</div>
	{/each}
</div>

<style>
	.src-menu {
		position: fixed;
		z-index: 90;
		width: 190px;
		padding: 8px 9px 9px;
		background: var(--rb-well, #0a0c0f);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		line-height: 1.35;
		text-align: left;
	}
	.src-head {
		margin: 0 0 6px;
		font-weight: 650;
		letter-spacing: 0.02em;
		color: var(--rb-text-dim);
	}
	.src-sub {
		margin: 0 0 6px;
		color: var(--rb-text-dim);
	}
	.src-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin: 0 0 4px;
	}
	.src-label {
		display: flex;
		align-items: center;
		gap: 6px;
		color: var(--rb-text);
	}
	.src-show {
		display: inline-flex;
		align-items: center;
		gap: 2px;
		font-size: 9px;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.src-err {
		font-size: 9px;
		color: var(--rb-text);
		text-decoration: underline wavy;
	}
	.src-show input {
		margin: 0;
		width: 10px;
		height: 10px;
		accent-color: var(--rb-accent);
		cursor: pointer;
	}
	.src-seg {
		display: flex;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		overflow: hidden;
	}
	.src-seg button {
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 9px;
		letter-spacing: 0.04em;
		padding: 2px 6px;
		cursor: pointer;
	}
	.src-seg button.active {
		background: var(--rb-accent);
		color: var(--rb-on-accent, #fff);
	}
	.src-seg button:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}
</style>
