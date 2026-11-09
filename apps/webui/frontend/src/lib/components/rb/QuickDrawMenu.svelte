<script lang="ts">
	import { quickDrawClaimsContextMenu } from '$lib/components/rb/quick-draw-target';
	// Performance quick-draw: right-click opens a two-column menu.
	// Left of the click = always-on tall Unload / Loop tree. At the click =
	// contextual actions derived from the target (deck-scoped for now).
	import { onMount, tick } from 'svelte';
	import { clampToViewport } from '$lib/ui/clamp-to-viewport';
	import { deckAutoGain, DECK_IDS, engine, getDeckState, mixerState } from '$lib/rb/audio-engine.svelte';
	import {
		dispatchPerformanceCommand,
		queryPerformanceState,
		runPerformanceCommandFromUi,
		type PerformanceCommand
	} from '$lib/rb/performance-ipc.svelte';
	import { deckMoveMenuEntries } from '$lib/rb/deck-move';
	import {
		applyDeckMenuLabels,
		deckLoadSelectionCommand,
		deckMoveLabels,
		deckTrackInfoCommand
	} from '$lib/context-menu/deck-menu-commands';
	import { runReanalyze } from '$lib/components/rb/browser/track-context-menu';
	import { rustCommandUnsupported } from '$lib/audio-engine/rust-mode.svelte';
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
		setTransitionInCue,
		startStemGeneration,
		type StemTier
	} from '$lib/rb/api-rb';
	import { getTrack } from '$lib/api';
	import { pushToast } from '$lib/stores.svelte';
	import { vocalFixMenuItem } from './vocal-correction-menu';
	import { runTransitionInCue } from './transition-in-menu';
	import { getBrowseAdapter } from '$lib/rb/midi/browse-adapter';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { openSettings } from '$lib/settings/overlay.svelte';
	import type { PerfView } from '$lib/rb/topbar-view-prefs';
	import { waveClickAt } from './wave-click';
	import { transitionTargetOk } from '$lib/rb/auto-play-next.svelte';
	import { isUsbTrackId } from '$lib/rb/track-source';
	import { menuStemTiers } from '$lib/rb/stem-tier-menu';
	import { menuFor, type ResolvedMenuItem } from '$lib/context-menu/registry';
	import {
		autoGainFactOf,
		controlClassOf,
		controlKillCommand,
		controlResetCommand,
		controlValueOf,
		stemLevelsResetCommands,
		trimAutoGainCommands
	} from '$lib/context-menu/mixer-actions';
	import {
		stableIdFromTarget,
		targetOf,
		type ElementLike,
		type QuickDrawTarget
	} from '$lib/context-menu/target-of';
	import { createDeckHotCueActions } from '$lib/rb/deck-hot-cue-actions';
	import { ANIMATE_CONTROLS, animationKeyOf, type AnimateControl } from '$lib/rb/animate-expr';
	import { STEM_CONTROL_IDS, type StemControl } from '$lib/rb/stem-types';
	import { cancelAnimation, dialAnimations, openAnimatePrompt } from '$lib/rb/dial-animations.svelte';
	import AnimatePrompt from './AnimatePrompt.svelte';

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
				if (open && stemTargetId !== null && lastMenuTarget !== null) {
					ctx = _itemsForTarget(lastMenuTarget, null, null);
				}
			})
			.catch((e) => {
				// Fail visibly: a silently empty ladder looks like "no stems here".
				console.error('[quick-draw] stem ladder unavailable', e);
			});
	});

	/** The row the open menu belongs to, so a late estimate can refresh labels. */
	let stemTargetId = $state<string | null>(null);
	/** Last resolved target so a stem estimate refresh rebuilds the same menu. */
	let lastMenuTarget = $state<QuickDrawTarget | null>(null);

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
		if (open && stemTargetId === stableId && lastMenuTarget !== null) {
			ctx = _itemsForTarget(lastMenuTarget, null, null);
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

	function _asElementLike(t: EventTarget | null): ElementLike | null {
		return t instanceof Element ? (t as unknown as ElementLike) : null;
	}

	function _targetOf(t: EventTarget | null, knownStableId: string | null): QuickDrawTarget {
		return targetOf(_asElementLike(t), knownStableId);
	}

	function _stableIdFromTarget(t: EventTarget | null): string | null {
		return stableIdFromTarget(_asElementLike(t));
	}

	async function _sendAll(commands: Record<string, unknown>[]): Promise<void> {
		for (const command of commands) {
			await runPerformanceCommandFromUi(command as PerformanceCommand);
		}
	}

	function _stemPart(control: AnimateControl): StemControl | null {
		if (!control.startsWith('stem_')) return null;
		const part = control.slice('stem_'.length);
		return (STEM_CONTROL_IDS as readonly string[]).includes(part) ? (part as StemControl) : null;
	}

	function _controlRaw(target: Extract<QuickDrawTarget, { kind: 'mixer-control' }>): number {
		const { deck, control } = target;
		if (control === 'crossfader') return mixerState.crossfader;
		if (deck === null) throw new Error(`control ${control} needs a deck`);
		const ch = mixerState.channels[deck];
		if (control === 'eq_low') return ch.eq_low;
		if (control === 'eq_mid') return ch.eq_mid;
		if (control === 'eq_high') return ch.eq_high;
		if (control === 'trim') return ch.trim;
		if (control === 'filter') return ch.filter;
		if (control === 'fader') return ch.fader;
		const part = _stemPart(control);
		if (part === null) throw new Error(`no raw value for ${control}`);
		return getDeckState(deck).stems.controls[part].gain;
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

	function _runnerIfSupported(type: string, run: CtxItem['run']): CtxItem['run'] | undefined {
		return rustCommandUnsupported(type) ? undefined : run;
	}

	function _deckItems(
		target: Extract<QuickDrawTarget, { kind: 'deck' | 'waveform' }>,
		event: MouseEvent | null,
		eventTarget: EventTarget | null
	): CtxItem[] {
		const deck = target.deck;
		const state = getDeckState(deck);
		const loaded = state.stable_id !== null;
		const stableId = state.stable_id;
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
			const perf = queryPerformanceState();
			const selection = perf.browser.selected_row !== null;
			const otherLoaded = DECK_IDS.filter((d) => d !== deck && getDeckState(d).stable_id !== null);
			const moveEntries = deckMoveMenuEntries(deck, DECK_IDS, (d) => getDeckState(d));
			const moveLabels = deckMoveLabels(moveEntries);
			for (const { to, force } of moveEntries) {
				const id = `move-to-${to}`;
				const moveRun = _runnerIfSupported('deck_move', (pressT0Ms) =>
					runPerformanceCommandFromUi(
						{ type: 'deck_move', from: deck, to, mode: 'move', ...(force ? { force: true } : {}) },
						pressT0Ms
					).then(() => {})
				);
				if (moveRun !== undefined) runners[id] = moveRun;
			}
			for (const other of DECK_IDS) {
				if (other === deck) continue;
				const id = `double-from-${other}`;
				const doubleRun = _runnerIfSupported('deck_move', (pressT0Ms) =>
					runPerformanceCommandFromUi({ type: 'deck_move', from: other, to: deck, mode: 'copy' }, pressT0Ms).then(
						() => {}
					)
				);
				if (doubleRun !== undefined) runners[id] = doubleRun;
			}
			// PRRT_kwDOSEvNd860wjZw: plain load only; playing-deck guard in performance-ipc.
			runners['load-selection'] = (pressT0Ms) => {
				const command = deckLoadSelectionCommand(deck, queryPerformanceState().browser.selected_row);
				if (command === null) return Promise.resolve();
				return runPerformanceCommandFromUi(command, pressT0Ms).then(() => {});
			};
			const keyLockRun = _runnerIfSupported('master_tempo', (pressT0Ms) =>
				_runAction('master_tempo.toggle', deck, pressT0Ms)
			);
			if (keyLockRun !== undefined) runners['key-lock'] = keyLockRun;
			const quantizeRun = _runnerIfSupported('quantize', (pressT0Ms) =>
				_runAction('quantize.toggle', deck, pressT0Ms)
			);
			if (quantizeRun !== undefined) runners['quantize'] = quantizeRun;
			const slipRun = _runnerIfSupported('slip', (pressT0Ms) => _runAction('slip.toggle', deck, pressT0Ms));
			if (slipRun !== undefined) runners['slip'] = slipRun;
			if (stableId !== null) {
				runners['find-in-library'] = (pressT0Ms) =>
					runPerformanceCommandFromUi(
						{ type: 'browser_select_playlist', playlist_id: 'all', stable_id: stableId },
						pressT0Ms
					).then(() => {});
				runners['track-info'] = (pressT0Ms) =>
					runPerformanceCommandFromUi(deckTrackInfoCommand(deck), pressT0Ms).then(() => {});
				runners['reanalyze'] = async () => {
					await runReanalyze([stableId]);
				};
			}
			return applyDeckMenuLabels(
				menuFor(target, {
					loaded,
					playing: state.playing,
					looping: state.loop !== null,
					synced: state.beat_sync_enabled,
					master: state.is_master,
					selection,
					keyLock: state.master_tempo_enabled,
					quantize: state.quantize_enabled,
					slip: state.slip_enabled,
					otherLoaded
				}),
				moveLabels
			).map((item) => _toCtxItem(item, runners[item.id]));
		}
		const vocal = event !== null ? vocalFixMenuItem(event, eventTarget) : null;
		const click = event !== null ? waveClickAt(event, eventTarget) : null;
		const cueAt = click === null ? 'none' : isUsbTrackId(click.stableId) ? 'stick' : 'position';
		if (click !== null) {
			// AGENT-51: the engine snaps the click to the nearest downbeat and says which bar.
			// A refusal (no beatgrid, unknown track) is toasted, never swallowed.
			runners['transition-in'] = () =>
				runTransitionInCue(click, {
					setTransitionInCue,
					pushToast,
					deckIds: DECK_IDS,
					getDeckState,
					refreshHotCues: (id) => engine.refreshHotCues(id)
				});
		}
		const outTargets = DECK_IDS.filter((other) => other !== deck && transitionTargetOk(other));
		if (click !== null) {
			for (const other of outTargets) {
				runners[`transition-out-${other}`] = (pressT0Ms) =>
					runPerformanceCommandFromUi(
						{ type: 'auto_play_next_arm', out_deck: deck, in_deck: other, from_ms: click.timeS * 1000 },
						pressT0Ms
					).then(() => {});
			}
		}
		return menuFor(target, { loaded, looping: state.loop !== null, vocalHit: vocal !== null, cueAt, outTargets }).map((item) => {
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
		const fact = autoGainFactOf(deckAutoGain[deck]);
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
			'assign-thru': () => send({ type: 'assign', deck, assign: 'THRU' }),
			'trim-auto-gain': () =>
				_sendAll(
					trimAutoGainCommands(deck, fact, ch.trim === 0.5, getDeckState(deck).stable_id)
				)
		};
		return menuFor(target, {
			eqFlat: ch.eq_low === 0.5 && ch.eq_mid === 0.5 && ch.eq_high === 0.5,
			filterFlat: ch.filter === 0.5,
			cue: ch.cue_enabled,
			assign: ch.assign,
			trimUnity: ch.trim === 0.5,
			autoGain: fact
		}).map((item) => _toCtxItem(item, runners[item.id]));
	}

	function _controlItems(target: Extract<QuickDrawTarget, { kind: 'mixer-control' }>): CtxItem[] {
		const key = animationKeyOf(ANIMATE_CONTROLS[target.control].command(target.deck ?? 1, 0));
		if (key === null) throw new Error(`no animation key for ${target.control}`);
		const raw = _controlRaw(target);
		const autoGain =
			target.deck === null ? ('none' as const) : autoGainFactOf(deckAutoGain[target.deck]);
		const resolved = menuFor(
			{ kind: 'mixer-control', label: target.label },
			{
				animating: dialAnimations.running[key] !== undefined,
				control: controlClassOf(target.control),
				value: controlValueOf(target.control, raw),
				autoGain
			}
		);
		const runners: Record<string, CtxItem['run']> = {
			animate: async () => openAnimatePrompt(target.deck, target.control, x, y),
			'animate-stop': async () => {
				cancelAnimation(key, 'stopped from the menu');
			}
		};
		for (const item of resolved) {
			if (item.id.startsWith('reset-')) {
				runners[item.id] = () =>
					runPerformanceCommandFromUi(controlResetCommand(target.control, target.deck) as PerformanceCommand).then(
						() => {}
					);
			} else if (item.id === 'eq-kill') {
				runners[item.id] = () =>
					runPerformanceCommandFromUi(
						controlKillCommand(target.control, target.deck ?? 1) as PerformanceCommand
					).then(() => {});
			} else if (item.id === 'trim-auto-gain' && target.deck !== null) {
				const deck = target.deck;
				runners[item.id] = () =>
					_sendAll(
						trimAutoGainCommands(deck, autoGain, raw === 0.5, getDeckState(deck).stable_id)
					);
			}
		}
		const items = resolved.map((item) => _toCtxItem(item, runners[item.id]));
		if (target.channel === null) return items;
		const dialIds = new Set(items.map((item) => item.id));
		const channelItems = _mixerItems({ kind: 'mixer-channel', deck: target.channel }).filter(
			(item) => !dialIds.has(item.id)
		);
		return [...items, ...channelItems];
	}

	function _stemLaneItems(target: Extract<QuickDrawTarget, { kind: 'stem' }>): CtxItem[] {
		const deck = target.deck;
		const state = getDeckState(deck);
		const s = state.stems;
		const ctrl = s.controls[target.stem];
		const items: CtxItem[] = [];
		const menuCtx = {
			loaded: state.stable_id !== null,
			stems: s.status === 'ready' ? ('ready' as const) : ('none' as const),
			available: s.available_controls.includes(target.stem),
			muted: ctrl.muted,
			solo: ctrl.solo,
			levelsUnity: STEM_CONTROL_IDS.every((id) => s.controls[id].gain === 0.5)
		};
		const runners: Record<string, CtxItem['run']> = {
			'stem-mute': () =>
				runPerformanceCommandFromUi({
					type: 'stem_mute',
					deck,
					stem: target.stem,
					muted: !ctrl.muted
				}).then(() => {}),
			'stem-solo': () =>
				runPerformanceCommandFromUi({
					type: 'stem_solo',
					deck,
					stem: target.stem,
					solo: !ctrl.solo
				}).then(() => {}),
			'stem-levels-reset': () =>
				_sendAll(stemLevelsResetCommands(deck, s.available_controls))
		};
		for (const item of menuFor(target, menuCtx)) {
			if (item.id === 'stems-tiers') {
				if (item.enabled && state.stable_id !== null) {
					items.push(..._stemItems(state.stable_id));
				} else {
					items.push(_toCtxItem(item, undefined));
				}
				continue;
			}
			items.push(_toCtxItem(item, runners[item.id]));
		}
		return items;
	}

	function _itemsForTarget(
		menuTarget: QuickDrawTarget,
		event: MouseEvent | null,
		eventTarget: EventTarget | null
	): CtxItem[] {
		switch (menuTarget.kind) {
			case 'track-chip': {
				stemTargetId = menuTarget.stableId;
				return _trackChipItems(menuTarget.stableId);
			}
			case 'deck':
			case 'waveform':
				return _deckItems(menuTarget, event, eventTarget);
			case 'hot-cue':
				return _hotCueItems(menuTarget);
			case 'mixer-channel':
				return _mixerItems(menuTarget);
			case 'mixer-control':
				return _controlItems(menuTarget);
			case 'stem': {
				const stemState = getDeckState(menuTarget.deck);
				if (stemState.stable_id !== null) {
					stemTargetId = stemState.stable_id;
				}
				return _stemLaneItems(menuTarget);
			}
			case 'tree-readonly':
				// A USB stick's tree opens its own menu (UsbStickTree, USBEXP-04)
				// and stops the event, so only stick-less trees reach here.
				return menuFor({ kind: 'tree-readonly' }, { stick: false }).map((item) => _toCtxItem(item, undefined));
			case 'library-source':
				// LibrarySourceMenu stops the event; this fallback has no runners.
				return menuFor({ kind: 'library-source' }, {}).map((item) => _toCtxItem(item, undefined));
			case 'perf-empty': {
				const browser = getBrowseAdapter() !== null;
				const ctx = { browser, view: uiPrefs.perf_view as PerfView };
				const runners: Record<string, CtxItem['run']> = {
					settings: async () => openSettings()
				};
				for (const deck of DECK_IDS) {
					runners[`load-selected-${deck}`] = async () => {
						const adapter = getBrowseAdapter();
						if (adapter === null) {
							pushToast('Select a track in the browser first', 'error');
							return;
						}
						adapter.loadSelected(deck);
					};
				}
				for (const view of ['waveform', 'list', 'phase-scope', 'phase-meter'] as const) {
					runners[`view-${view}`] = async () => {
						await runPerformanceCommandFromUi({ type: 'set_perf_view', view });
					};
				}
				return menuFor({ kind: 'perf-empty' }, ctx).map((item) => _toCtxItem(item, runners[item.id]));
			}
		}
	}

	function _contextItems(
		target: EventTarget | null,
		knownStableId: string | null = null,
		event: MouseEvent | null = null
	): CtxItem[] {
		const menuTarget = _targetOf(target, knownStableId);
		lastMenuTarget = menuTarget;
		if (menuTarget.kind === 'track-chip' && knownStableId === null) {
			void _loadStemEstimates(menuTarget.stableId).catch((e) => {
				console.error('[quick-draw] stem estimates unavailable', e);
				stemEstimateById = { _error: String(e) };
			});
		}
		if (menuTarget.kind === 'stem') {
			const state = getDeckState(menuTarget.deck);
			const s = state.stems;
			const stemMenuCtx = {
				loaded: state.stable_id !== null,
				stems: s.status === 'ready' ? ('ready' as const) : ('none' as const),
				available: s.available_controls.includes(menuTarget.stem),
				muted: s.controls[menuTarget.stem].muted,
				solo: s.controls[menuTarget.stem].solo,
				levelsUnity: STEM_CONTROL_IDS.every((id) => s.controls[id].gain === 0.5)
			};
			const stemsTiers = menuFor(menuTarget, stemMenuCtx).find((i) => i.id === 'stems-tiers');
			if (stemsTiers?.enabled && state.stable_id !== null) {
				void _loadStemEstimates(state.stable_id).catch((e) => {
					console.error('[quick-draw] stem estimates unavailable', e);
					stemEstimateById = { _error: String(e) };
				});
			}
		}
		return _itemsForTarget(menuTarget, event, target);
	}

	function onContextMenu(e: MouseEvent): void {
		const t = e.target;
		if (!(t instanceof Element) || !quickDrawClaimsContextMenu(t)) return;
		e.preventDefault();
		e.stopPropagation();
		// W28: library source tabs open LibrarySourceMenu and stop the event;
		// read-only trees with no items still suppress Quick Draw (CTXMENU-06).
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
		if (getDeckState(deck).stable_id === null) return;
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

<AnimatePrompt />

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
							{@const loaded = getDeckState(deck).stable_id !== null}
							<button
								type="button"
								class="qd-item qd-tall"
								class:qd-inert={!loaded}
								role="menuitem"
								disabled={!loaded}
								title={loaded ? undefined : 'no track loaded'}
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
	.qd-item:disabled {
		cursor: not-allowed;
	}
	.qd-empty {
		padding: 8px 10px;
		color: var(--rb-text-dim, #838990);
	}
</style>
