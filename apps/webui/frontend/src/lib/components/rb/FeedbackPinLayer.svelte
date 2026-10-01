<script lang="ts">
	/**
	 * Global comment-pin layer (FB-16): markers, placement overlay, draft bubble,
	 * open card, Option-gated badges, and anchor highlight. Mounted once from
	 * +layout.svelte so pins work on every route and over root overlays.
	 */
	import { afterNavigate } from '$app/navigation';
	import { onMount, untrack } from 'svelte';
	import {
		describeAnchor,
		followOnText,
		isPinDrawn,
		markPinSeen,
		pinFromClient,
		type PinDraft,
		type PinSeen
	} from '$lib/rb/feedback';
	import { isPinOnPage, parseShowHarvestedPins, SHOW_HARVESTED_PINS_KEY } from '$lib/rb/feedback-pin-board';
	import { readPinsVisible, writePinsVisible, onPinsVisibleChanged } from '$lib/rb/feedback-pin-visibility';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { readPinSeen, writePinSeen } from '$lib/rb/feedback-pin-seen';
	import { readParkedPinDraft } from '$lib/rb/feedback-pin-draft-restore';
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
	import { OVERLAY_Z_INDEX } from '$lib/overlays/overlay-stack';
	import FeedbackPinCard from './FeedbackPinCard.svelte';
	import FeedbackPinDraftBubble from './FeedbackPinDraftBubble.svelte';
	import FeedbackPinMarkers from './feedback/FeedbackPinMarkers.svelte';

	let pinDraft:
		| (PinDraft & {
				viewport: { width: number; height: number };
				followOn: { parentId: string; label: string } | null;
		  })
		| null = $state(null);
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
	let openPinId: string | null = $state(null);
	let pinSeen: PinSeen = $state({});
	let pinsVisible: boolean = $state(false);
	let showHarvestedPins: boolean = $state(true);
	let optionKeyHeld = $state(false);

	const pagePins = $derived(
		pinsVisible
			? feedbackState.pins.filter((p) => {
					if (!isPinDrawn(p)) return false;
					if (!isPinOnPage(p, pathname)) return false;
					if (p.author === 'agent' && !uiPrefs.show_agent_pins) return false;
					if (p.status === 'harvested' && !showHarvestedPins) return false;
					return true;
				})
			: []
	);
	const bodyPin = $derived(pagePins.find((p) => p.id === openPinId) ?? null);
	$effect(() => {
		if (!draftHydrated) return;
		persistParkedPinDraft(localStorage, pinDraft, foreignDraftParked, (err) => {
			pushToast(
				`comment draft could not be saved: ${err instanceof Error ? err.message : String(err)}`,
				'error',
				undefined,
				err,
				{ source: 'feedback-pin-draft-storage' }
			);
		});
	});

	$effect(() => {
		if (!draftHydrated) return;
		if (feedbackState.pendingDraft === null) return;
		const draft = untrack(() => takePendingPinDraft());
		if (draft === null) return;
		foreignDraftParked = false;
		pinDraft = {
			point: draft.point,
			anchor: draft.anchor,
			text: draft.text,
			page: draft.page,
			viewport: draft.viewport ?? _currentViewport(),
			followOn: null
		};
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
		pinDraft = draft;
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
			window.removeEventListener('keydown', onOptionDown);
			window.removeEventListener('keyup', onOptionUp);
			window.removeEventListener('blur', onBlur);
			window.removeEventListener('pagehide', flush);
		};
	});

	function openPin(pin: FeedbackPin): void {
		openPinId = pin.id;
	}

	function closePin(): void {
		if (bodyPin !== null) _writeSeen(markPinSeen(pinSeen, bodyPin));
		openPinId = null;
	}

	async function archiveOpenPin(pin: FeedbackPin): Promise<void> {
		if (await archivePin(pin.id)) openPinId = null;
	}

	function startFollowOn(pin: FeedbackPin): void {
		openPinId = null;
		foreignDraftParked = false;
		pinDraft = {
			point: { x_pct: pin.x_pct, y_pct: pin.y_pct },
			anchor: pin.anchor,
			viewport: { width: window.innerWidth, height: window.innerHeight },
			text: '',
			page: pathname,
			followOn: { parentId: pin.id, label: followOnText(pin).trim() }
		};
		void focusPinDraftTextarea();
	}

	async function focusPinDraftTextarea(): Promise<void> {
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
		disarmPinPlacement();
		foreignDraftParked = false;
		pinDraft = {
			point,
			anchor: describeAnchor(under ?? null),
			viewport: { width: window.innerWidth, height: window.innerHeight },
			text: '',
			page: pathname,
			followOn: null
		};
		void focusPinDraftTextarea();
	}

	function handleEscape(e: KeyboardEvent): void {
		if (e.key !== 'Escape') return;
		if (feedbackState.placementArmed) disarmPinPlacement();
		else if (pinDraft !== null) pinDraft = null;
		else if (openPinId !== null) closePin();
	}
</script>

<svelte:window onkeydown={handleEscape} />

<FeedbackPinMarkers
	pins={pagePins}
	seen={pinSeen}
	{pathname}
	{optionKeyHeld}
	onopen={openPin}
/>

{#if bodyPin !== null}
	<FeedbackPinCard
		pin={bodyPin}
		onclose={closePin}
		onarchive={() => archiveOpenPin(bodyPin)}
		onfollowon={() => startFollowOn(bodyPin)}
		onreply={(text) => addReply(bodyPin.id, text)}
	/>
{/if}

{#if feedbackState.placementArmed}
	<button
		type="button"
		class="fb-place-overlay"
		style:z-index={OVERLAY_Z_INDEX.feedbackDock}
		aria-label="Click to place the comment pin, Escape to cancel"
		onpointerdowncapture={handlePlacementPointerDown}
	></button>
{/if}

<FeedbackPinDraftBubble bind:pinDraft bind:this={draftBubble} {pushToast} />

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
