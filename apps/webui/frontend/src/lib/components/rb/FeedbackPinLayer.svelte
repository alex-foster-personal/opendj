<script lang="ts">
	/**
	 * Global comment-pin layer (FB-16): markers, placement overlay, draft bubble,
	 * open card, Option-gated badges, and anchor highlight. Mounted once from
	 * +layout.svelte so pins work on every route and over root overlays.
	 */
	import { afterNavigate } from '$app/navigation';
	import { onMount, tick, untrack } from 'svelte';
	import {
		describeAnchor,
		followOnText,
		isPinDrawn,
		markPinSeen,
		pinFromClient,
		type PinDraft,
		type PinSeen
	} from '$lib/rb/feedback';
	import { isPinWithinVisibleDays } from '$lib/rb/feedback-pin-age';
	import { isPinOnPage, parseShowHarvestedPins, SHOW_HARVESTED_PINS_KEY } from '$lib/rb/feedback-pin-board';
	import { readPinsVisible, writePinsVisible, onPinsVisibleChanged } from '$lib/rb/feedback-pin-visibility';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { readPinSeen, writePinSeen } from '$lib/rb/feedback-pin-seen';
	import { readParkedPinDraft, restoreParkedPinDraft } from '$lib/rb/feedback-pin-draft-restore';
	import { persistParkedPinDraft } from '$lib/rb/feedback-pin-draft-persist';
	import { pushToast } from '$lib/stores.svelte';
	import {
		addReply,
		archivePin,
		disarmPinPlacement,
		feedbackState,
		flushFeedbackSaves,
		hydrateFeedback,
		startPinWatch,
		stopPinWatch,
		takePendingPinDraft,
		type FeedbackPin
	} from '$lib/rb/feedback-store.svelte';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import {
		applyPinAnchorHighlight,
		clearPinAnchorHighlight
	} from '$lib/rb/feedback-pin-anchor-highlight';
	import { resolvePinAnchorAt } from '$lib/rb/feedback-pin-placement';
	import {
		capturePinPlacement,
		measureSelector,
		resolvePinPosition,
		type PinPlacement,
		type ResolvedPinPosition
	} from '$lib/rb/feedback-pin-position';
	import { OVERLAY_Z_INDEX } from '$lib/overlays/overlay-stack';
	import FeedbackPinCard from './FeedbackPinCard.svelte';
	import FeedbackPinDraftBubble from './FeedbackPinDraftBubble.svelte';
	import FeedbackPinMarkers from './feedback/FeedbackPinMarkers.svelte';

	/** One composer each. A second comment used to overwrite this and drop the draft. */
	type OpenPinDraft = PinDraft & {
		viewport: { width: number; height: number };
		followOn: { parentId: string; label: string } | null;
		placement: PinPlacement | null;
		key: number;
		stack: number;
	};
	const MORE_PIN_DRAFTS_KEY = 'odj-feedback-pin-draft-more';
	let pinDrafts: (OpenPinDraft | null)[] = $state([]);
	let nextDraftKey = 1;
	function _currentViewport(): { width: number; height: number } {
		return { width: window.innerWidth, height: window.innerHeight };
	}
	let draftBubble: { focusPinDraftTextarea: () => Promise<void> } | undefined = $state();
	let draftHydrated = $state(false);
	let foreignDraftParked = $state(false);
	let pathname = $state('/');
	afterNavigate(({ to }) => {
		// `to` is null only when navigating out of the app; keep the last route.
		if (to !== null) pathname = to.url.pathname;
	});
	let openPinIds: string[] = $state([]);
	let pinSeen: PinSeen = $state({});
	let pinsVisible: boolean = $state(false);
	let showHarvestedPins: boolean = $state(true);
	let optionKeyHeld = $state(false);

	/* Saved visibility, a hover peek, or armed placement. Writing the
	   comment (the draft bubble) hides the markers again unless the
	   checkbox is on. Pin 2230ee193a61. */
	const pagePins = $derived(
		pinsVisible ||
		feedbackState.pinsPeek ||
		feedbackState.placementArmed
			? feedbackState.pins.filter((p) => {
					if (!isPinDrawn(p)) return false;
					if (!isPinOnPage(p, pathname)) return false;
					if (p.author === 'agent' && !uiPrefs.show_agent_pins) return false;
					if (p.status === 'harvested' && !showHarvestedPins) return false;
					if (!isPinWithinVisibleDays(p.created_at, uiPrefs.pin_visible_days, Date.now())) return false;
					return true;
				})
			: []
	);
	const openPins = $derived(
		openPinIds
			.map((id) => pagePins.find((p) => p.id === id) ?? null)
			.filter((p): p is FeedbackPin => p !== null)
	);
	const bodyPin = $derived(openPins[openPins.length - 1] ?? null);

	/** Bumped on resize, scroll, and a 1 s tick while pins are drawn, so a pin
	 * re-places itself when the element it is tagged to moves or goes away
	 * (feedback-pin-position.ts). Percent-only pins never needed this. */
	let layoutTick = $state(0);
	const pinPositions: Map<string, ResolvedPinPosition> = $derived.by(() => {
		void layoutTick;
		const positions = new Map<string, ResolvedPinPosition>();
		if (typeof window === 'undefined' || pagePins.length === 0) return positions;
		const viewport = { w: window.innerWidth, h: window.innerHeight };
		for (const pin of pagePins) positions.set(pin.id, resolvePinPosition(pin, measureSelector, viewport));
		return positions;
	});
	function _draftSaveError(err: unknown): void {
		pushToast(
			`comment draft could not be saved: ${err instanceof Error ? err.message : String(err)}`,
			'error',
			undefined,
			err,
			{ source: 'feedback-pin-draft-storage' }
		);
	}

	function _readMoreDrafts(page: string, stackFrom: number): OpenPinDraft[] {
		try {
			const raw = localStorage.getItem(MORE_PIN_DRAFTS_KEY);
			if (raw === null || raw === '') return [];
			const parsed = JSON.parse(raw) as unknown;
			if (!Array.isArray(parsed)) return [];
			const loaded: OpenPinDraft[] = [];
			for (const item of parsed) {
				const { draft } = restoreParkedPinDraft(JSON.stringify(item), page, _currentViewport());
				if (draft === null) continue;
				loaded.push({ ...draft, key: nextDraftKey, stack: stackFrom + loaded.length });
				nextDraftKey += 1;
			}
			return loaded;
		} catch (err) {
			pushToast(
				`comment draft could not be restored: ${err instanceof Error ? err.message : String(err)}`,
				'error',
				undefined,
				err,
				{ source: 'feedback-pin-draft-storage' }
			);
			return [];
		}
	}

	/** Append a composer. Never replaces one that is already open. */
	function pushPinDraft(draft: Omit<OpenPinDraft, 'key' | 'stack'>): void {
		foreignDraftParked = false;
		const stack = pinDrafts.filter((open) => open !== null).length;
		pinDrafts = [...pinDrafts, { ...draft, key: nextDraftKey, stack }];
		nextDraftKey += 1;
	}

	$effect(() => {
		if (!draftHydrated) return;
		const live = pinDrafts.filter((d): d is OpenPinDraft => d !== null);
		for (const draft of live) void draft.text;
		const withText = live.filter((draft) => draft.text.trim() !== '');
		// A new empty composer must not wipe a typed one from storage.
		persistParkedPinDraft(localStorage, withText[0] ?? null, foreignDraftParked, _draftSaveError);
		if (foreignDraftParked && withText.length === 0) return;
		try {
			const rest = withText.slice(1);
			if (rest.length === 0) localStorage.removeItem(MORE_PIN_DRAFTS_KEY);
			else localStorage.setItem(MORE_PIN_DRAFTS_KEY, JSON.stringify(rest));
		} catch (err) {
			_draftSaveError(err);
		}
	});

	$effect(() => {
		if (!pinDrafts.some((d) => d === null)) return;
		pinDrafts = pinDrafts.filter((d) => d !== null);
	});

	$effect(() => {
		if (!draftHydrated) return;
		if (feedbackState.pendingDraft === null) return;
		const draft = untrack(() => takePendingPinDraft());
		if (draft === null) return;
		pushPinDraft({
			point: draft.point,
			anchor: draft.anchor,
			text: draft.text,
			page: draft.page,
			viewport: draft.viewport ?? _currentViewport(),
			followOn: null,
			placement: draft.placement ?? null
		});
		void focusPinDraftTextarea();
	});

	$effect(() => {
		const pin = bodyPin;
		if (pin === null || pin === undefined) {
			clearPinAnchorHighlight();
			return;
		}
		applyPinAnchorHighlight(pin.anchor);
		return () => clearPinAnchorHighlight();
	});

	function _globals(): Record<string, unknown> {
		return window as unknown as Record<string, unknown>;
	}

	function _readSeen(): PinSeen {
		try {
			return readPinSeen(window.localStorage);
		} catch {
			return {};
		}
	}

	function _writeSeen(next: PinSeen): void {
		pinSeen = next;
		try {
			writePinSeen(window.localStorage, next);
		} catch {
			// storage blocked
		}
	}

	function markSeen(pinId: string): void {
		const pin = feedbackState.pins.find((p) => p.id === pinId);
		if (pin !== undefined) _writeSeen(markPinSeen(pinSeen, pin));
	}

	function _writePinsVisible(next: boolean): void {
		pinsVisible = next;
		writePinsVisible(window.localStorage, next);
	}

	onMount(() => {
		pathname = window.location.pathname;
		const { draft, foreign } = readParkedPinDraft(
			localStorage,
			pathname,
			_currentViewport(),
			(err) => {
				pushToast(
					`comment draft could not be restored: ${err instanceof Error ? err.message : String(err)}`,
					'error',
					undefined,
					err,
					{ source: 'feedback-pin-draft-storage' }
				);
			}
		);
		const restored: OpenPinDraft[] = [];
		if (draft !== null) {
			restored.push({ ...draft, key: nextDraftKey, stack: 0 });
			nextDraftKey += 1;
		}
		if (!foreign) restored.push(..._readMoreDrafts(pathname, restored.length));
		pinDrafts = restored;
		foreignDraftParked = foreign;
		draftHydrated = true;
		bootScheduler.defer('feedback:hydrate', () => {
			void hydrateFeedback();
		});
		startPinWatch();
		pinSeen = _readSeen();
		pinsVisible = readPinsVisible(window.localStorage);
		const offVis = onPinsVisibleChanged(() => {
			pinsVisible = readPinsVisible(window.localStorage);
		});
		showHarvestedPins = parseShowHarvestedPins(window.localStorage.getItem(SHOW_HARVESTED_PINS_KEY));
		_globals().__mdtPinSeen = {
			get: () => ({ ...pinSeen }),
			markSeen: (id: string) => markSeen(id)
		};
		_globals().__mdtPinsVisible = {
			get: () => pinsVisible,
			set: (value: boolean) => _writePinsVisible(value)
		};
		_globals().__mdtPinPlacementArmed = {
			get: () => feedbackState.placementArmed
		};
		// Agent parity for pin re-placement: which tier each drawn pin resolved
		// on (element | anchor | viewport), the selector it used, and whether
		// that is a fallback. `refresh: true` re-measures first.
		_globals().__mdtPinPositions = {
			get: (opts?: { refresh?: boolean }) => {
				if (opts?.refresh) layoutTick += 1;
				return Object.fromEntries(pinPositions);
			}
		};

		let relayoutQueued = false;
		const relayout = (): void => {
			if (relayoutQueued) return;
			relayoutQueued = true;
			requestAnimationFrame(() => {
				relayoutQueued = false;
				layoutTick += 1;
			});
		};
		const relayoutTimer = setInterval(() => {
			if (pagePins.length > 0 && document.visibilityState === 'visible') layoutTick += 1;
		}, 1000);
		window.addEventListener('resize', relayout);
		window.addEventListener('scroll', relayout, { capture: true, passive: true });

		const onOptionDown = (e: KeyboardEvent): void => {
			if (e.key === 'Alt') optionKeyHeld = true;
		};
		const onOptionUp = (e: KeyboardEvent): void => {
			if (e.key === 'Alt') optionKeyHeld = false;
		};
		const onBlur = (): void => {
			optionKeyHeld = false;
		};
		window.addEventListener('keydown', onOptionDown);
		window.addEventListener('keyup', onOptionUp);
		window.addEventListener('blur', onBlur);

		const flush = () => flushFeedbackSaves();
		window.addEventListener('pagehide', flush);
		return () => {
			offVis();
			stopPinWatch();
			flushFeedbackSaves();
			clearPinAnchorHighlight();
			delete _globals().__mdtPinSeen;
			delete _globals().__mdtPinsVisible;
			delete _globals().__mdtPinPlacementArmed;
			delete _globals().__mdtPinPositions;
			clearInterval(relayoutTimer);
			window.removeEventListener('resize', relayout);
			window.removeEventListener('scroll', relayout, { capture: true });
			window.removeEventListener('keydown', onOptionDown);
			window.removeEventListener('keyup', onOptionUp);
			window.removeEventListener('blur', onBlur);
			window.removeEventListener('pagehide', flush);
		};
	});

	function openPin(pin: FeedbackPin): void {
		if (openPinIds.includes(pin.id)) return;
		openPinIds = [...openPinIds, pin.id];
	}

	function closePin(pinId: string | null = null): void {
		const id = pinId ?? openPinIds[openPinIds.length - 1] ?? null;
		const bodyPin = openPins.find((p) => p.id === id) ?? null;
		if (bodyPin !== null) _writeSeen(markPinSeen(pinSeen, bodyPin));
		if (id !== null) openPinIds = openPinIds.filter((openId) => openId !== id);
	}

	async function archiveOpenPin(pin: FeedbackPin): Promise<void> {
		if (await archivePin(pin.id)) openPinIds = openPinIds.filter((id) => id !== pin.id);
	}

	function startFollowOn(pin: FeedbackPin): void {
		pushPinDraft({
			point: { x_pct: pin.x_pct, y_pct: pin.y_pct },
			anchor: pin.anchor,
			viewport: { width: window.innerWidth, height: window.innerHeight },
			text: '',
			page: pathname,
			followOn: { parentId: pin.id, label: followOnText(pin).trim() },
			// The daemon copies the parent's placement onto the follow-on.
			placement: null
		});
		void focusPinDraftTextarea();
	}

	async function focusPinDraftTextarea(): Promise<void> {
		await tick();
		await draftBubble?.focusPinDraftTextarea();
	}

	function handlePlacementPointerDown(e: PointerEvent): void {
		e.preventDefault();
		e.stopPropagation();
		const point = pinFromClient(
			e.clientX,
			e.clientY,
			window.innerWidth,
			window.innerHeight
		);
		const under = resolvePinAnchorAt(e.clientX, e.clientY, (x, y) =>
			document.elementsFromPoint(x, y)
		);
		const anchor = describeAnchor(under ?? null);
		// resolvePinAnchorAt types its result as the DOM-free AnchorishElement
		// for node:test; here it is always the real element elementsFromPoint returned.
		const placement = capturePinPlacement({ x: e.clientX, y: e.clientY }, under as Element | null, anchor);
		disarmPinPlacement();
		pushPinDraft({
			point,
			anchor,
			viewport: { width: window.innerWidth, height: window.innerHeight },
			text: '',
			page: pathname,
			followOn: null,
			placement
		});
		void focusPinDraftTextarea();
	}

	function handleEscape(e: KeyboardEvent): void {
		if (e.key !== 'Escape') return;
		if (feedbackState.placementArmed) disarmPinPlacement();
		else if (pinDrafts.some((d) => d !== null)) {
			let last = -1;
			for (let i = 0; i < pinDrafts.length; i += 1) {
				if (pinDrafts[i] !== null) last = i;
			}
			if (last >= 0) pinDrafts[last] = null;
		} else if (openPinIds.length > 0) closePin();
	}
</script>

<svelte:window onkeydown={handleEscape} />

<FeedbackPinMarkers
	pins={pagePins}
	positions={pinPositions}
	seen={pinSeen}
	{pathname}
	{optionKeyHeld}
	onopen={openPin}
/>

{#each openPins as pin (pin.id)}
	<FeedbackPinCard
		{pin}
		point={pinPositions.get(pin.id) ?? null}
		onclose={() => closePin(pin.id)}
		onarchive={() => archiveOpenPin(pin)}
		onfollowon={() => startFollowOn(pin)}
		onreply={(text) => addReply(pin.id, text)}
	/>
{/each}

{#if feedbackState.placementArmed}
	<button
		type="button"
		class="fb-place-overlay"
		style:z-index={OVERLAY_Z_INDEX.feedbackDock}
		aria-label="Click to place the comment pin, Escape to cancel"
		onpointerdowncapture={handlePlacementPointerDown}
	></button>
{/if}

{#each pinDrafts as draft, i (draft?.key ?? `closed-${i}`)}
	{#if draft !== null}
		<FeedbackPinDraftBubble bind:pinDraft={pinDrafts[i]} bind:this={draftBubble} {pushToast} />
	{/if}
{/each}

<style>
	:global(.fb-pin-anchor-target) {
		outline: 1px dotted #fff;
		outline-offset: 1px;
		animation: fb-pin-anchor-pulse 1.2s ease-in-out infinite;
	}
	@keyframes fb-pin-anchor-pulse {
		0%,
		100% {
			outline-color: rgba(255, 255, 255, 0.55);
		}
		50% {
			outline-color: rgba(255, 255, 255, 1);
		}
	}

	.fb-place-overlay {
		position: fixed;
		inset: 0;
		background: transparent;
		border: none;
		cursor: crosshair;
		padding: 0;
	}
</style>
