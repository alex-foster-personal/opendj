<script lang="ts">
	// Performance quick-draw: right-click opens a two-column menu.
	// Left of the click = always-on tall Unload / Loop tree. At the click =
	// contextual actions derived from the target (deck-scoped for now).
	import { onMount, tick } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import { DECK_IDS, getDeckState, mixerState } from '$lib/rb/audio-engine.svelte';
	import {
		dispatchPerformanceCommand,
		runPerformanceCommandFromUi
	} from '$lib/rb/performance-ipc.svelte';
	import { createLoadBlendController } from '$lib/rb/load-blend-dispatch';
	import LoadBlendHud from './LoadBlendHud.svelte';
	import {
		quickDrawCommand,
		type QuickDrawActionId
	} from '$lib/rb/quick-draw-catalog';
	import type { DeckId } from '$lib/rb/deck-slots';
	import { setMenuHighlightStableId } from '$lib/rb/quick-draw-ui.svelte';
	import {
		fetchStemEstimates,
		fetchStemTiers,
		startStemGeneration,
		type StemTier
	} from '$lib/rb/api-rb';
	import { getTrack } from '$lib/api';
	import { pushToast } from '$lib/stores.svelte';
	import { vocalFixMenuItem } from './vocal-correction-menu';
	import { menuStemTiers } from '$lib/rb/stem-tier-menu';
	import { menuFor, type CueSlot, type MenuTarget, type ResolvedMenuItem } from '$lib/context-menu/registry';
	import { createDeckHotCueActions } from '$lib/rb/deck-hot-cue-actions';

	type CtxItem = {
		id: string;
		label: string;
		/** pressT0Ms is the triggering click's own event.timeStamp, Q1's
		 * operator-felt press stamp - undefined for actions outside the P0
		 * press paths (load, stems), which ignore it. */
		run: (pressT0Ms?: number) => Promise<void>;
		/** Inert rungs render dimmed and explain themselves on hover. */
		disabled?: boolean;
		title?: string;
		testId?: string;
		/** A toggle or choice item's state from the engine read model; renders
		 * as menuitemcheckbox with a check mark. Undefined for plain actions. */
		checked?: boolean;
		/** Present only on Load to CHn; pointer handlers own the click-drag blend. */
		loadDeck?: DeckId;
		stableId?: string;
	};
	type Root = 'unload' | 'loop' | 'play';

	// QuickDrawMenuLoader fetches this module on the first right-click and
	// hands that click over, so the menu opens for it instead of for the next.
	let { initialEvent = null }: { initialEvent?: MouseEvent | null } = $props();
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
				// The menu can already be open (the loader replays the click that
				// fetched this module), and its ctx was built before the ladder
				// landed, so rebuild it or that first open never shows the stems.
				if (open && stemTargetId !== null) ctx = _contextItems(null, stemTargetId);
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
		// INSTALL-32: a tier this engine cannot spawn is hidden, not disabled.
		return menuStemTiers(stemTiers).map((tier) => {
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

	let blendHud = $state({ visible: false, t: 0, fader: 0, scrubDeltaMs: 0 });
	/** True after Load pointerdown so the synthetic click cannot double-load. */
	let loadPressArmed = false;
	const blend = createLoadBlendController({
		run: async (cmd) => {
			await dispatchPerformanceCommand(cmd);
		},
		getDeck: (deck) => getDeckState(deck),
		getChannel: (deck) => mixerState.channels[deck],
		toast: pushToast,
		hud: blendHud
	});

	function _masterDeck(): DeckId | null {
		return DECK_IDS.find((d) => getDeckState(d).is_master) ?? null;
	}

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

	const CUE_SLOTS: readonly CueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

	/**
	 * W28: name what was right-clicked, so the registry (not this component)
	 * decides the items. Most specific first: a hot-cue pad and a mixer strip
	 * sit inside deck-ish markup, and a track row can sit inside the browser.
	 */
	type QuickDrawTarget =
		| Extract<MenuTarget, { kind: 'hot-cue' | 'mixer-channel' | 'waveform' | 'deck' }>
		| { kind: 'track-chip'; stableId: string }
		| { kind: 'tree-readonly' | 'library-source' | 'perf-empty' };

	function _deckAttr(el: Element | null, attr: string): DeckId | null {
		const n = Number(el?.getAttribute(attr));
		return DECK_IDS.includes(n as DeckId) ? (n as DeckId) : null;
	}

	function _targetOf(t: EventTarget | null, knownStableId: string | null): QuickDrawTarget {
		if (knownStableId !== null) return { kind: 'track-chip', stableId: knownStableId };
		if (!(t instanceof Element)) return { kind: 'perf-empty' };
		const cue = t.closest('[data-hot-cue-slot]');
		const cueDeck = _deckAttr(cue?.closest('[data-deck]') ?? null, 'data-deck');
		const slot = cue?.getAttribute('data-hot-cue-slot') as CueSlot | null | undefined;
		if (cueDeck !== null && slot != null && CUE_SLOTS.includes(slot)) {
			return { kind: 'hot-cue', deck: cueDeck, slot };
		}
		const mixerDeck = _deckAttr(t.closest('[data-mixer-channel]'), 'data-mixer-channel');
		if (mixerDeck !== null) return { kind: 'mixer-channel', deck: mixerDeck };
		// A read-only tree or source strip owns everything inside it: a child
		// carrying a stable id there (a stick track, say) must not borrow the
		// track-chip actions. A marker only wins over a stable id it encloses.
		const markedEl = t.closest('[data-menu-target]');
		const marked = markedEl?.getAttribute('data-menu-target');
		const stableEl = t.closest('[data-stable-id]');
		if (
			(marked === 'tree-readonly' || marked === 'library-source') &&
			(stableEl === null || markedEl!.contains(stableEl))
		) {
			return { kind: marked };
		}
		const stableId = _stableIdFromTarget(t);
		if (stableId !== null) return { kind: 'track-chip', stableId };
		const deck = _deckFromTarget(t);
		if (deck !== null) {
			return t.closest('[data-wave-surface]') !== null ? { kind: 'waveform', deck } : { kind: 'deck', deck };
		}
		return { kind: 'perf-empty' };
	}

	function _toCtxItem(item: ResolvedMenuItem, run: CtxItem['run'] | undefined): CtxItem {
		const ctxItem: CtxItem = {
			id: item.id,
			label: item.label,
			disabled: !item.enabled || run === undefined,
			title: item.enabled && run === undefined ? `not available in this view - ${item.title}` : item.title,
			run: run ?? (async () => {})
		};
		if (item.testId !== undefined) ctxItem.testId = item.testId;
		if (item.checked !== undefined) ctxItem.checked = item.checked;
		return ctxItem;
	}

	function _trackChipItems(stableId: string): CtxItem[] {
		// A chip names a track but carries no availability, so it is resolved
		// as present; a missing file still fails loudly in the load path.
		const items: CtxItem[] = [];
		for (const item of menuFor({ kind: 'track-chip' }, { audio: 'present' })) {
			if (item.id === 'stems-tiers') {
				items.push(..._stemItems(stableId));
				continue;
			}
			const deck = Number(item.id.slice('load-'.length)) as DeckId;
			const ctxItem = _toCtxItem(item, async () => {
				if (getDeckState(deck).stable_id !== null) {
					await runPerformanceCommandFromUi({ type: 'unload', deck });
				}
				await runPerformanceCommandFromUi({ type: 'load', deck, stable_id: stableId });
			});
			items.push({ ...ctxItem, id: `${item.id}-${stableId}`, loadDeck: deck, stableId });
		}
		return items;
	}

	function _deckItems(
		target: Extract<QuickDrawTarget, { kind: 'deck' | 'waveform' }>,
		event: MouseEvent | null,
		eventTarget: EventTarget | null
	): CtxItem[] {
		const deck = target.deck;
		const state = getDeckState(deck);
		const loaded = state.stable_id !== null;
		const runners: Record<string, CtxItem['run']> = {
			play: (pressT0Ms) => _togglePlay(deck, pressT0Ms),
			unload: (pressT0Ms) => _runAction('unload', deck, pressT0Ms),
			'exit-loop': (pressT0Ms) => _runAction('loop.exit', deck, pressT0Ms),
			'beat-sync': (pressT0Ms) =>
				runPerformanceCommandFromUi(
					{ type: 'beat_sync', deck, enabled: !getDeckState(deck).beat_sync_enabled },
					pressT0Ms
				).then(() => {}),
			'make-master': (pressT0Ms) => _runAction('master', deck, pressT0Ms)
		};
		if (target.kind === 'deck') {
			return menuFor(target, {
				loaded,
				playing: state.playing,
				looping: state.loop !== null,
				synced: state.beat_sync_enabled,
				master: state.is_master
			}).map((item) => _toCtxItem(item, runners[item.id]));
		}
		const vocal = event !== null ? vocalFixMenuItem(event, eventTarget) : null;
		return menuFor(target, { loaded, looping: state.loop !== null, vocalHit: vocal !== null }).map((item) => {
			if (item.id !== 'vocal-fix-comment' || vocal === null) return _toCtxItem(item, runners[item.id]);
			// The vocal item keeps its own live probe of the feedback API.
			const ctxItem = _toCtxItem(item, vocal.run);
			ctxItem.disabled = vocal.disabled;
			if (vocal.title !== undefined) ctxItem.title = vocal.title;
			return ctxItem;
		});
	}

	function _hotCueItems(target: Extract<QuickDrawTarget, { kind: 'hot-cue' }>): CtxItem[] {
		const { deck, slot } = target;
		const state = getDeckState(deck);
		const actions = createDeckHotCueActions(() => deck, () => getDeckState(deck));
		const cue = state.hot_cues.find((c) => c.slot === slot);
		const runners: Record<string, CtxItem['run']> = {
			'cue-jump': (pressT0Ms) =>
				runPerformanceCommandFromUi({ type: 'hot_cue_trigger', deck, slot }, pressT0Ms).then(() => {}),
			// A move keeps the cue's name; the registry disables it when the cue
			// has a loop or color the save command cannot carry.
			'cue-save': () => actions.saveHotCueAt(slot, cue?.comment ?? undefined).then(() => {}),
			'cue-clear': () => actions.clearHotCueAt(slot).then(() => {})
		};
		return menuFor(target, {
			loaded: state.stable_id !== null,
			filled: cue !== undefined,
			moveLoses: cue === undefined ? 'nothing' : cue.is_loop ? 'loop' : cue.color_table_index !== null ? 'color' : 'nothing'
		}).map((item) => _toCtxItem(item, runners[item.id]));
	}

	function _mixerItems(target: Extract<QuickDrawTarget, { kind: 'mixer-channel' }>): CtxItem[] {
		const deck = target.deck;
		const ch = mixerState.channels[deck];
		const send = (command: Parameters<typeof runPerformanceCommandFromUi>[0]) =>
			runPerformanceCommandFromUi(command).then(() => {});
		const runners: Record<string, CtxItem['run']> = {
			// 0.5 is flat for every EQ band and bypass for the filter (mixer-types.ts).
			'eq-reset': async () => {
				for (const band of ['low', 'mid', 'high'] as const) await send({ type: 'eq', deck, band, value: 0.5 });
			},
			'filter-reset': () => send({ type: 'filter', deck, value: 0.5 }),
			'channel-cue': () => send({ type: 'channel_cue', deck, enabled: !ch.cue_enabled }),
			'assign-a': () => send({ type: 'assign', deck, assign: 'A' }),
			'assign-b': () => send({ type: 'assign', deck, assign: 'B' }),
			'assign-thru': () => send({ type: 'assign', deck, assign: 'THRU' })
		};
		return menuFor(target, {
			eqFlat: ch.eq_low === 0.5 && ch.eq_mid === 0.5 && ch.eq_high === 0.5,
			filterFlat: ch.filter === 0.5,
			cue: ch.cue_enabled,
			assign: ch.assign
		}).map((item) => _toCtxItem(item, runners[item.id]));
	}

	function _contextItems(
		target: EventTarget | null,
		knownStableId: string | null = null,
		event: MouseEvent | null = null
	): CtxItem[] {
		const menuTarget = _targetOf(target, knownStableId);
		switch (menuTarget.kind) {
			case 'track-chip': {
				const stableId = menuTarget.stableId;
				stemTargetId = stableId;
				if (knownStableId === null) void _loadStemEstimates(stableId).catch((e) => {
					console.error('[quick-draw] stem estimates unavailable', e);
					stemEstimateById = { _error: String(e) };
				});
				return _trackChipItems(stableId);
			}
			case 'deck':
			case 'waveform':
				return _deckItems(menuTarget, event, target);
			case 'hot-cue':
				return _hotCueItems(menuTarget);
			case 'mixer-channel':
				return _mixerItems(menuTarget);
			case 'tree-readonly':
			case 'library-source':
			case 'perf-empty':
				return menuFor({ kind: menuTarget.kind }, {}).map((item) => _toCtxItem(item, undefined));
		}
	}

	function onContextMenu(e: MouseEvent): void {
		const t = e.target;
		if (!(t instanceof Element) || t.closest('.perf-root') === null) return;
		e.preventDefault();
		e.stopPropagation();
		// W28: a library surface whose registry menu is empty gets no menu at
		// all, rather than the deck launcher (which is not about that target).
		const kind = _targetOf(t, null).kind;
		if (kind === 'tree-readonly' || kind === 'library-source') {
			_close();
			return;
		}
		x = e.clientX;
		y = e.clientY;
		// A deck-target menu has no row, so a late ladder or estimate for an
		// earlier row must not relabel it.
		stemTargetId = null;
		ctx = _contextItems(t, null, e);
		root = 'unload';
		loopLeaf = null;
		setMenuHighlightStableId(_stableIdFromTarget(t));
		leaveArmed = false;
		open = true;
	}

	function _close(): void {
		if (blend.isActive()) return;
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
		if (blend.isActive()) return;
		const next = e.relatedTarget;
		if (next instanceof Node && menuEl?.contains(next)) return;
		_close();
	}

	function onKeydown(e: KeyboardEvent): void {
		if (e.key !== 'Escape' || !open) return;
		if (blend.isActive()) blend.abort();
		_close();
	}

	function onLoadPointerDown(e: PointerEvent, item: CtxItem): void {
		if (item.loadDeck === undefined || item.stableId === undefined) return;
		if (e.button !== 0) return;
		e.preventDefault();
		e.stopPropagation();
		loadPressArmed = true;
		const btn = e.currentTarget;
		if (btn instanceof HTMLButtonElement) btn.setPointerCapture(e.pointerId);
		const deck = item.loadDeck;
		const stableId = item.stableId;
		blend.down(e.clientX, e.clientY, deck, stableId, _masterDeck(), e.pointerId, () =>
			item.run()
		);
	}

	async function _runAction(id: QuickDrawActionId, deck: DeckId, pressT0Ms?: number): Promise<void> {
		_close();
		await runPerformanceCommandFromUi(quickDrawCommand(id, deck), pressT0Ms);
	}

	async function _togglePlay(deck: DeckId, pressT0Ms?: number): Promise<void> {
		_close();
		const playing = getDeckState(deck).playing;
		await runPerformanceCommandFromUi({ type: 'play', deck, playing: !playing }, pressT0Ms);
	}

	function _setRoot(next: Root): void {
		root = next;
		loopLeaf = null;
	}

	function _menuUnionRect(): DOMRect | null {
		if (menuEl == null) return null;
		const selectors = ['.qd', '.qd-quick', '.qd-mid', '.qd-leaf', '.qd-ctx'];
		let minX = Infinity;
		let minY = Infinity;
		let maxX = -Infinity;
		let maxY = -Infinity;
		for (const selector of selectors) {
			const node = menuEl.querySelector(selector);
			if (!(node instanceof HTMLElement)) continue;
			const rect = node.getBoundingClientRect();
			if (rect.width <= 0 || rect.height <= 0) continue;
			minX = Math.min(minX, rect.left);
			minY = Math.min(minY, rect.top);
			maxX = Math.max(maxX, rect.right);
			maxY = Math.max(maxY, rect.bottom);
		}
		if (!Number.isFinite(minX)) return menuEl.getBoundingClientRect();
		return new DOMRect(minX, minY, maxX - minX, maxY - minY);
	}

	async function _clampMenuPosition(): Promise<void> {
		await tick();
		const union = _menuUnionRect();
		if (union === null) return;
		const box = clampToViewport(
			union.left,
			union.top,
			{ width: union.width, height: union.height },
			{ width: window.innerWidth, height: window.innerHeight }
		);
		x += box.x - union.left;
		y += box.y - union.top;
	}

	$effect(() => {
		if (!open) return;
		void _clampMenuPosition();
	});

	onMount(() => {
		const onPointerDown = (e: PointerEvent): void => {
			if (!open) return;
			if (blend.isActive()) return;
			if (e.target instanceof Node && menuEl?.contains(e.target)) return;
			_close();
		};
		window.addEventListener('pointerdown', onPointerDown, true);
		if (initialEvent !== null) onContextMenu(initialEvent);
		return () => window.removeEventListener('pointerdown', onPointerDown, true);
	});
</script>

<svelte:window oncontextmenu={onContextMenu} onkeydown={onKeydown} />

{#if open}
	<div
		class="qd"
		data-testid="quick-draw-menu"
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
								onclick={(e) => void _runAction('unload', deck, e.timeStamp)}
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
								onclick={(e) => void _togglePlay(deck, e.timeStamp)}
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
									onclick={(e) => void _runAction(leaf, deck, e.timeStamp)}
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
						role={item.checked === undefined ? 'menuitem' : 'menuitemcheckbox'}
						aria-checked={item.checked}
						data-testid={item.testId}
						disabled={item.disabled === true}
						title={item.title ?? null}
						onpointerdown={(e) => {
							if (item.loadDeck === undefined) return;
							onLoadPointerDown(e, item);
						}}
						onpointermove={(e) => {
							if (item.loadDeck === undefined) return;
							blend.move(e.clientX, e.clientY, e.pointerId);
						}}
						onpointerup={(e) => {
							if (item.loadDeck === undefined) return;
							blend.up(e.pointerId);
							_close();
						}}
						onlostpointercapture={(e) => {
							if (item.loadDeck === undefined) return;
							blend.lostCapture(e.pointerId);
							_close();
						}}
						onclick={(e) => {
							if (item.disabled === true) return;
							if (item.loadDeck !== undefined) {
								if (loadPressArmed) {
									loadPressArmed = false;
									return;
								}
							}
							_close();
							void item.run(e.timeStamp);
						}}
					>
						{#if item.checked !== undefined}<svg class="qd-check" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true"
								>{#if item.checked}<path d="M2 6.5l2.5 2.5L10 3.5" fill="none" stroke="currentColor" stroke-width="1.6" />{/if}</svg
							>{/if}{item.label}
					</button>
				{/each}
			{/if}
		</div>
	</div>
	{#if blendHud.visible}
		<LoadBlendHud t={blendHud.t} fader={blendHud.fader} scrubDeltaMs={blendHud.scrubDeltaMs} />
	{/if}
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

	.qd-check {
		flex: none;
		margin-right: 4px;
		vertical-align: -1px;
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
		color: var(--rb-text-dim, #838990);
	}
</style>
