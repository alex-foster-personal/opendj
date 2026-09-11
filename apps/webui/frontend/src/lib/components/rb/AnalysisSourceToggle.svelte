<script lang="ts">
	/**
	 * PARITY-02: top-left rbx-vs-own source toggle. A per-feature A/B switch
	 * for testing our own-rolled analysis lanes against rekordbox's, backed
	 * by GET/PUT /api/v1/analysis/source (analysis-source.svelte.ts), which
	 * this control drives through the in-memory dev toggle half only. That
	 * toggle itself is never persisted and always resets to 'unset' on
	 * relaunch, but the EFFECTIVE source it falls back to is the lane's
	 * persisted default - 'own' if the lane has been promoted, 'rbx'
	 * otherwise (discussion_r3972682741 P2 NON-BLOCKING) - so a relaunch does
	 * not always land back on rekordbox.
	 */
	import {
		ANALYSIS_SOURCE_FEATURES,
		type AnalysisSource,
		analysisSourceState,
		loadAnalysisSource,
		subscribeAnalysisRecordChanges
	} from '$lib/rb/analysis-source.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';

	const FEATURE_LABEL: Record<string, string> = {
		beatgrid: 'Beatgrid',
		key: 'Key',
		waveform: 'Waveform',
		loudness: 'Loudness',
		vocal: 'Vocal'
	};

	const UNSERVED_OWN_TIP: Record<string, string> = {
		waveform: "waveform lane has no serving implementation yet",
		vocal: "vocal lane has no serving implementation yet"
	};

	function _ownDisabled(feature: (typeof ANALYSIS_SOURCE_FEATURES)[number]): boolean {
		const lane = feature;
		return !analysisSourceState.serving.includes(lane);
	}

	function _ownTitle(feature: (typeof ANALYSIS_SOURCE_FEATURES)[number]): string {
		if (!_ownDisabled(feature)) return 'Select own analysis for this lane';
		return UNSERVED_OWN_TIP[feature] ?? `lane '${feature}' has no serving implementation yet`;
	}

	// Live-but-not-noisy, matching usb-tracker.svelte.ts / feedback-store.svelte.ts's
	// POLL_MS. An agent driving PUT /api/v1/analysis/source directly (this
	// endpoint's whole agent-native-parity point) mutates the daemon with no
	// event this tab hears - a one-shot mount fetch would leave the visible
	// control mislabeling whichever source is actually being served
	// (discussion_r3921666947).
	const POLL_MS = 5000;

	let menuOpen = $state(false);
	let wrapEl: HTMLSpanElement | undefined = $state();
	let menuStyle = $state('');

	// This control owns the analysis-source lifecycle for the route, so it also
	// owns the OWN-grid invalidation subscription: a re-analysis replaces the
	// record `/anlz` derives an own grid from, and nothing else would notice
	// (see subscribeAnalysisRecordChanges).
	// Reported, not discarded. `loadAnalysisSource` throws through on a failed
	// deck refresh by design, and `void`-ing it made every such failure an
	// unhandled rejection with nothing said to anyone (the same shape
	// discussion_r3969942725 flagged on the record-change path). The poll
	// itself is the retry: the next tick re-reads the daemon and the deck
	// watermark still disagrees, so the refresh runs again.
	const _poll = (): void => {
		void loadAnalysisSource().catch((exc) => {
			console.error('[analysis-source] poll failed; retrying on the next tick', exc);
		});
	};

	$effect(() => {
		_poll();
		const id = setInterval(_poll, POLL_MS);
		const unsubscribe = subscribeAnalysisRecordChanges();
		return () => {
			clearInterval(id);
			unsubscribe();
		};
	});

	const anyOwn = $derived(
		ANALYSIS_SOURCE_FEATURES.some((feature) => analysisSourceState.features[feature] === 'own')
	);

	// Flush against the wrapper's bottom edge, NOT offset below it. The menu
	// is position:fixed (the topbar is overflow:hidden), so any gap between
	// the two boxes is uncovered screen: a pointer crossing it fires the
	// wrapper's own pointerleave with a relatedTarget that is neither the
	// wrapper nor the menu, and _hide closes the menu before the pointer can
	// reach RBX or OWN. Keyboard focus was unaffected, which is why the
	// source-level positioning looked fine (discussion_r3968534392 P1
	// BLOCKING). Contiguous boxes make the crossing a wrapper -> menu
	// transition, which _hide already treats as staying inside.
	function _show(): void {
		if (wrapEl !== undefined) {
			const rect = wrapEl.getBoundingClientRect();
			menuStyle = `left:${Math.round(rect.left)}px;top:${Math.round(rect.bottom)}px`;
		}
		menuOpen = true;
	}

	function _hide(e: FocusEvent | PointerEvent): void {
		const next =
			e instanceof FocusEvent ? e.relatedTarget : (e as PointerEvent).relatedTarget;
		if (next instanceof Node && wrapEl?.contains(next)) return;
		if (next instanceof Element && next.closest?.('.src-menu')) return;
		menuOpen = false;
	}

	function _pick(feature: (typeof ANALYSIS_SOURCE_FEATURES)[number], source: AnalysisSource): void {
		void runPerformanceCommandFromUi({ type: 'analysis_source', feature, source });
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<span
	class="src-wrap"
	bind:this={wrapEl}
	onpointerenter={_show}
	onpointerleave={_hide}
	onfocusin={_show}
	onfocusout={_hide}
>
	<button
		type="button"
		class="src-toggle"
		class:on={anyOwn}
		aria-haspopup="true"
		aria-expanded={menuOpen}
		title="rbx-vs-own source A/B: pick which lane each own-rolled feature reads from. Testing/dev only - this toggle itself resets on relaunch, back to the lane's persisted default (own if promoted, rbx otherwise), not always rekordbox."
	>
		SOURCE
		<svg width="7" height="5" viewBox="0 0 7 5" aria-hidden="true">
			<path d="M0.5 1 L3.5 4 L6.5 1" fill="none" stroke="currentColor" stroke-width="1.2" />
		</svg>
	</button>
	{#if menuOpen}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="src-menu"
			style={menuStyle}
			role="menu"
			tabindex="-1"
			aria-label="Analysis source"
			onpointerenter={_show}
			onpointerleave={() => (menuOpen = false)}
		>
			<p class="src-head">Source (testing - resets on relaunch)</p>
			{#each ANALYSIS_SOURCE_FEATURES as feature (feature)}
				{@const current = analysisSourceState.features[feature] ?? 'rekordbox'}
				<div class="src-row">
					<span class="src-label">{FEATURE_LABEL[feature] ?? feature}</span>
					<div class="src-seg" role="group" aria-label={`${feature} source`}>
						<button
							type="button"
							class:active={current === 'rekordbox'}
							onclick={() => _pick(feature, 'rekordbox')}
						>
							RBX
						</button>
						<button
							type="button"
							class:active={current === 'own'}
							disabled={_ownDisabled(feature)}
							title={_ownTitle(feature)}
							onclick={() => _pick(feature, 'own')}
						>
							OWN
						</button>
					</div>
				</div>
			{/each}
		</div>
	{/if}
</span>

<style>
	.src-wrap {
		position: relative;
		display: inline-flex;
		align-items: center;
	}
	.src-toggle {
		display: flex;
		align-items: center;
		gap: 4px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		letter-spacing: 0.05em;
		padding: 2px 8px;
		line-height: 1;
		cursor: pointer;
	}
	.src-toggle.on {
		color: var(--rb-accent);
		border-color: color-mix(in srgb, var(--rb-accent) 55%, var(--rb-border));
	}
	.src-menu {
		position: fixed;
		z-index: 90;
		width: 190px;
		padding: 8px 9px 9px;
		background: #0a0c0f;
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
	.src-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin: 0 0 4px;
	}
	.src-label {
		color: var(--rb-text);
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
		color: #0a0c0f;
	}
	.src-seg button:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}
</style>
