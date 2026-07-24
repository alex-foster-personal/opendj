<script lang="ts">
	// Performance quick-draw: right-click opens a two-column menu.
	// Left of the click = always-on tall Unload / Loop tree. At the click =
	// contextual actions derived from the target (deck-scoped for now).
	import { onMount } from 'svelte';
	import { DECK_IDS, getDeckState } from '$lib/rb/audio-engine.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import {
		quickDrawCommand,
		type QuickDrawActionId
	} from '$lib/rb/quick-draw-catalog';
	import type { DeckId } from '$lib/rb/types';
	import { setMenuHighlightStableId } from '$lib/rb/quick-draw-ui.svelte';
	import {
		fetchStemEstimates,
		fetchStemTiers,
		startStemGeneration,
		type StemTier
	} from '$lib/rb/api-rb';
	import { getTrack } from '$lib/api';
	import { pushToast } from '$lib/stores.svelte';

	type CtxItem = {
		id: string;
		label: string;
		run: () => Promise<void>;
		/** Inert rungs render dimmed and explain themselves on hover. */
		disabled?: boolean;
		title?: string;
	};
	type Root = 'unload' | 'loop' | 'play';
	type LoopLeaf = 'loop.start_8' | 'loop.exit';

	let open = $state(false);
	let x = $state(0);
	let y = $state(0);
	let ctx = $state<CtxItem[]>([]);
	let root = $state<Root>('unload');
	let loopLeaf = $state<LoopLeaf | null>(null);
	let menuEl: HTMLDivElement | undefined = $state();

	// The stem ladder is static config, so fetch it once. Estimates are
	// per-track and land asynchronously; until they do, the label says
	// "measuring" rather than showing a number nobody measured.
	let stemTiers = $state<StemTier[]>([]);
	let stemEstimateById = $state<Record<string, string>>({});

	onMount(() => {
		void fetchStemTiers()
			.then((t) => {
				stemTiers = t;
			})
			.catch((e) => {
				// Fail visibly: a silently empty ladder looks like "no stems here".
				console.error('[quick-draw] stem ladder unavailable', e);
			});
	});

	/** The row the open menu belongs to, so a late estimate can refresh labels. */
	let stemTargetId = $state<string | null>(null);

	async function _loadStemEstimates(stableId: string): Promise<void> {
		stemEstimateById = {};
		const { track } = await getTrack(stableId);
		const durationS = (track.duration_ms ?? 0) / 1000;
		if (durationS <= 0) {
			stemEstimateById = { _error: 'no duration on this track' };
			return;
		}
		const { tiers } = await fetchStemEstimates(durationS);
		const next: Record<string, string> = {};
		for (const t of tiers) {
			next[t.tier] = t.measured
				? `${Math.round(t.seconds ?? 0)}s`
				: 'not measured';
		}
		stemEstimateById = next;
		// The ctx array was built BEFORE this resolved, so rebuild it or the
		// labels stay on their "..." placeholder forever. Only if the menu is
		// still open on the same row -- a stale refresh would relabel a menu
		// the user has since opened somewhere else.
		if (open && stemTargetId === stableId) {
			ctx = _contextItems(null, stableId);
		}
	}

	function _stemItems(stableId: string): CtxItem[] {
		return stemTiers.map((tier) => {
			const est = stemEstimateById[tier.key];
			const suffix = est === undefined ? '...' : est;
			const inert = tier.availability !== 'AVAILABLE';
			return {
				id: `stems-${tier.key}-${stableId}`,
				label: inert
					? `Stems: ${tier.name} (n/a)`
					: `Stems: ${tier.name} ~${suffix}`,
				disabled: inert,
				title: inert
					? tier.unavailable_because
					: `${tier.model} overlap ${tier.overlap}` +
						(tier.where === 'modal' ? ` on ${tier.gpu}` : ' on this Mac') +
						` -- ${tier.purpose}`,
				run: async () => {
					if (inert) return;
					const job = await startStemGeneration(stableId, tier.key);
					pushToast(
						`Stems ${tier.name}: job ${job.job_id} started`,
						'info'
					);
				}
			};
		});
	}
	/** Close-on-leave only after the pointer has entered the menu once. */
	let leaveArmed = false;

	function _deckFromTarget(t: EventTarget | null): DeckId | null {
		const el = t instanceof Element ? t.closest('[data-deck]') : null;
		if (el === null) return null;
		const n = Number(el.getAttribute('data-deck'));
		return DECK_IDS.includes(n as DeckId) ? (n as DeckId) : null;
	}

	function _stableIdFromTarget(t: EventTarget | null): string | null {
		const el = t instanceof Element ? t.closest('[data-stable-id]') : null;
		if (el === null) return null;
		const id = el.getAttribute('data-stable-id');
		return id !== null && id !== '' ? id : null;
	}

	function _contextItems(
		target: EventTarget | null,
		knownStableId: string | null = null
	): CtxItem[] {
		const stableId = knownStableId ?? _stableIdFromTarget(target);
		if (stableId !== null) {
			stemTargetId = stableId;
			if (knownStableId === null) void _loadStemEstimates(stableId).catch((e) => {
				console.error('[quick-draw] stem estimates unavailable', e);
				stemEstimateById = { _error: String(e) };
			});
			return [
				...DECK_IDS.map((deck) => ({
				id: `load-${deck}-${stableId}`,
				label: `Load to CH${deck}`,
				run: async () => {
					if (getDeckState(deck).stable_id !== null) {
						await runPerformanceCommandFromUi({ type: 'unload', deck });
					}
					await runPerformanceCommandFromUi({ type: 'load', deck, stable_id: stableId });
				}
			})),
				..._stemItems(stableId)
			];
		}
		const deck = _deckFromTarget(target);
		if (deck === null) return [];
		const items: CtxItem[] = [
			{
				id: `unload-${deck}`,
				label: `Unload CH${deck}`,
				run: () => _runAction('unload', deck)
			}
		];
		if (getDeckState(deck).loop !== null) {
			items.push({
				id: `exit-loop-${deck}`,
				label: `Exit Loop CH${deck}`,
				run: () => _runAction('loop.exit', deck)
			});
		}
		return items;
	}

	function onContextMenu(e: MouseEvent): void {
		const t = e.target;
		if (!(t instanceof Element) || t.closest('.perf-root') === null) return;
		e.preventDefault();
		e.stopPropagation();
		// Room for Unload/Play/Loop + mid tier + CH column.
		x = Math.min(Math.max(e.clientX, 320), window.innerWidth - 160);
		y = Math.min(Math.max(e.clientY, 8), window.innerHeight - 200);
		ctx = _contextItems(t);
		root = 'unload';
		loopLeaf = null;
		setMenuHighlightStableId(_stableIdFromTarget(t));
		leaveArmed = false;
		open = true;
	}

	function _close(): void {
		open = false;
		loopLeaf = null;
		leaveArmed = false;
		setMenuHighlightStableId(null);
	}

	function onMenuPointerEnter(): void {
		leaveArmed = true;
	}

	function onMenuPointerLeave(e: PointerEvent): void {
		if (!open || !leaveArmed) return;
		const next = e.relatedTarget;
		if (menuEl !== undefined && next instanceof Node && menuEl.contains(next)) return;
		_close();
	}

	function onKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape' && open) _close();
	}

	async function _runAction(id: QuickDrawActionId, deck: DeckId): Promise<void> {
		_close();
		await runPerformanceCommandFromUi(quickDrawCommand(id, deck));
	}

	async function _togglePlay(deck: DeckId): Promise<void> {
		_close();
		const playing = getDeckState(deck).playing;
		await runPerformanceCommandFromUi({ type: 'play', deck, playing: !playing });
	}

	function _setRoot(next: Root): void {
		root = next;
		loopLeaf = null;
	}

	onMount(() => {
		const onPointerDown = (e: PointerEvent): void => {
			if (!open) return;
			if (menuEl !== undefined && e.target instanceof Node && menuEl.contains(e.target)) return;
			_close();
		};
		window.addEventListener('pointerdown', onPointerDown, true);
		return () => window.removeEventListener('pointerdown', onPointerDown, true);
	});
</script>

<svelte:window oncontextmenu={onContextMenu} onkeydown={onKeydown} />

{#if open}
	<div
		class="qd"
		style:left="{x}px"
		style:top="{y}px"
		bind:this={menuEl}
		role="menu"
		onpointerdown={(e) => e.stopPropagation()}
		onpointerenter={onMenuPointerEnter}
		onpointerleave={onMenuPointerLeave}
	>
		<!-- Quick-draw sits LEFT of the click origin; deeper tiers further left. -->
		<div class="qd-quick">
			<div class="qd-mid">
				{#if root === 'unload'}
					<div class="qd-leaf" role="menu">
						{#each DECK_IDS as deck (deck)}
							<button
								type="button"
								class="qd-item qd-tall"
								role="menuitem"
								onclick={() => void _runAction('unload', deck)}
							>
								CH{deck}
							</button>
						{/each}
					</div>
				{:else if root === 'play'}
					<div class="qd-leaf" role="menu">
						{#each DECK_IDS as deck (deck)}
							<button
								type="button"
								class="qd-item qd-tall"
								role="menuitem"
								onclick={() => void _togglePlay(deck)}
							>
								{getDeckState(deck).playing ? 'Pause' : 'Play'} CH{deck}
							</button>
						{/each}
					</div>
				{:else}
					{#if loopLeaf !== null}
						{@const leaf = loopLeaf}
						<div class="qd-leaf" role="menu">
							{#each DECK_IDS as deck (deck)}
								<button
									type="button"
									class="qd-item qd-tall"
									role="menuitem"
									onclick={() => void _runAction(leaf, deck)}
								>
									CH{deck}
								</button>
							{/each}
						</div>
					{/if}
					<button
						type="button"
						class="qd-item qd-tall"
						class:open={loopLeaf === 'loop.start_8'}
						role="menuitem"
						onpointerenter={() => (loopLeaf = 'loop.start_8')}
						onclick={() => (loopLeaf = 'loop.start_8')}
					>
						Start Loop 8B
					</button>
					<button
						type="button"
						class="qd-item qd-tall"
						class:open={loopLeaf === 'loop.exit'}
						role="menuitem"
						onpointerenter={() => (loopLeaf = 'loop.exit')}
						onclick={() => (loopLeaf = 'loop.exit')}
					>
						Exit Loop
					</button>
				{/if}
			</div>

			<button
				type="button"
				class="qd-item qd-tall"
				class:open={root === 'unload'}
				role="menuitem"
				onpointerenter={() => _setRoot('unload')}
				onclick={() => _setRoot('unload')}
			>
				Unload
			</button>
			<button
				type="button"
				class="qd-item qd-tall"
				class:open={root === 'play'}
				role="menuitem"
				onpointerenter={() => _setRoot('play')}
				onclick={() => _setRoot('play')}
			>
				Play/Pause
			</button>
			<button
				type="button"
				class="qd-item qd-tall"
				class:open={root === 'loop'}
				role="menuitem"
				onpointerenter={() => _setRoot('loop')}
				onclick={() => _setRoot('loop')}
			>
				Loop
			</button>
		</div>

		<div class="qd-ctx">
			{#if ctx.length === 0}
				<span class="qd-empty">No target actions</span>
			{:else}
				{#each ctx as item (item.id)}
					<button
						type="button"
						class="qd-item"
						class:qd-inert={item.disabled === true}
						role="menuitem"
						disabled={item.disabled === true}
						title={item.title ?? null}
						onclick={() => {
							if (item.disabled === true) return;
							_close();
							void item.run();
						}}
					>
						{item.label}
					</button>
				{/each}
			{/if}
		</div>
	</div>
{/if}

<style>
	.qd {
		position: fixed;
		z-index: 10000;
		display: flex;
		align-items: stretch;
		pointer-events: auto;
		font-family: var(--rb-font, sans-serif);
		font-size: var(--rb-fs-browser, 11px);
		color: var(--rb-text, #c8cdd2);
	}
	.qd-quick,
	.qd-mid,
	.qd-leaf,
	.qd-ctx {
		padding: 2px;
		background: var(--rb-panel, #14171d);
		border: 1px solid rgba(180, 188, 198, 0.42);
		box-shadow: 0 10px 26px rgba(0, 0, 0, 0.58);
		border-radius: 3px;
	}
	.qd-quick {
		position: absolute;
		right: 100%;
		top: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		margin-right: 6px;
		min-width: 88px;
	}
	.qd-mid {
		position: absolute;
		right: 100%;
		top: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		margin-right: 6px;
		min-width: 120px;
	}
	.qd-leaf {
		position: absolute;
		right: 100%;
		top: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		margin-right: 6px;
		min-width: 72px;
	}
	.qd-ctx {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 140px;
	}
	.qd-inert {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.qd-item {
		display: block;
		width: 100%;
		text-align: left;
		padding: 6px 10px;
		border: none;
		border-radius: 2px;
		background: transparent;
		color: inherit;
		cursor: pointer;
		line-height: 1.2;
	}
	.qd-tall {
		min-height: 44px;
		font-weight: 600;
		letter-spacing: 0.02em;
	}
	.qd-item:hover,
	.qd-item.open {
		background: var(--rb-select, #1d3f73);
	}
	.qd-empty {
		padding: 8px 10px;
		color: var(--rb-text-dim, #7a8088);
	}
</style>
