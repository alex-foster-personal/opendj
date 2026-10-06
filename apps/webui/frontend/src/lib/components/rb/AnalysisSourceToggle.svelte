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
	import type { Component } from 'svelte';
	import { tick } from 'svelte';
	import { placeFloating } from '$lib/ui/clamp-to-viewport';
	import {
		ANALYSIS_SOURCE_FEATURES,
		type AnalysisSource,
		analysisSourceState,
		loadAnalysisSource,
		subscribeAnalysisRecordChanges
	} from '$lib/rb/analysis-source.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';

	type MenuComponent = Component<{
		style: string;
		onPick: (feature: (typeof ANALYSIS_SOURCE_FEATURES)[number], source: AnalysisSource) => void;
		onShow: () => void;
		onClose: () => void;
	}>;

	const POLL_MS = 5000;

	let menuOpen = $state(false);
	let wrapEl: HTMLSpanElement | undefined = $state();
	let menuStyle = $state('');
	let menuComponent = $state<MenuComponent | null>(null);
	let menuLoad: Promise<MenuComponent> | null = null;

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

	function _ensureMenu(): void {
		if (menuComponent !== null || menuLoad !== null) return;
		menuLoad = import('./AnalysisSourceMenu.svelte').then((mod) => {
			menuComponent = mod.default;
			return mod.default;
		});
	}

	async function _show(): Promise<void> {
		_ensureMenu();
		menuOpen = true;
		await tick();
		if (wrapEl === undefined) return;
		const rect = wrapEl.getBoundingClientRect();
		const menuNode = document.querySelector('.src-menu');
		const menuRect =
			menuNode instanceof HTMLElement
				? menuNode.getBoundingClientRect()
				: { width: 220, height: 160 };
		const box = placeFloating({
			trigger: {
				left: rect.left,
				top: rect.top,
				width: rect.width,
				height: rect.height
			},
			size: { width: menuRect.width, height: menuRect.height },
			viewport: { width: window.innerWidth, height: window.innerHeight },
			preferred: 'below',
			gap: 0
		});
		menuStyle = `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px`;
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
	data-custom-tip=""
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
		aria-label="Analysis source (rbx vs own)"
	>
		SOURCE
		<svg width="7" height="5" viewBox="0 0 7 5" aria-hidden="true">
			<path d="M0.5 1 L3.5 4 L6.5 1" fill="none" stroke="currentColor" stroke-width="1.2" />
		</svg>
	</button>
	{#if menuOpen && menuComponent !== null}
		<svelte:component
			this={menuComponent}
			style={menuStyle}
			onPick={_pick}
			onShow={_show}
			onClose={() => (menuOpen = false)}
		/>
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
</style>
