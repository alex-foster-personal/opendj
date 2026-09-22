<script lang="ts">
	/**
	 * In-app review/feedback widget (FB-01..FB-04): the topbar cluster.
	 *
	 * A down-chevron left of the vibe meter toggles the compact, draggable
	 * review-todo panel (FeedbackPanel.svelte); a comment icon arms one-shot
	 * comment-anywhere pin placement. Everything the user does here lands on
	 * /api/v1/feedback immediately (debounced for text) - the same endpoints
	 * agents use, so there is nothing UI-only to lose.
	 *
	 * Honest rendering: on a daemon with no /api/v1/feedback the chevron and
	 * comment icon render inert and name the control plus that this daemon
	 * does not serve feedback, never a broken panel.
	 */
	import { onMount, untrack } from 'svelte';
	import {
		describePinStatusSummary,
		describeAnchor,
		followOnText,
		isPinDrawn,
		markPinSeen,
		pinFromClient,
		type PinDraft,
		type PinSeen
	} from '$lib/rb/feedback';
	import {
		anchorMovedAtPin,
		parseShowHarvestedPins,
		pinBoardState,
		serializeShowHarvestedPins,
		SHOW_HARVESTED_PINS_KEY
	} from '$lib/rb/feedback-pin-board';
	import { readPinsVisible, writePinsVisible } from '$lib/rb/feedback-pin-visibility';
	import { setShowAgentPins, uiPrefs } from '$lib/rb/prefs.svelte';
	import { readPinSeen, writePinSeen } from '$lib/rb/feedback-pin-seen';
	import { readParkedPinDraft } from '$lib/rb/feedback-pin-draft-restore';
	import { persistParkedPinDraft } from '$lib/rb/feedback-pin-draft-persist';
	import { pushToast } from '$lib/stores.svelte';
	import {
		addReply,
		archivePin,
		armPinPlacement,
		disarmPinPlacement,
		feedbackState,
		flushFeedbackSaves,
		hydrateFeedback,
		startPinWatch,
		stopPinWatch,
		takePendingPinDraft,
		toggleFeedbackPanel,
		type FeedbackPin
	} from '$lib/rb/feedback-store.svelte';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import ControlExplainer from './deck/ControlExplainer.svelte';
	import FeedbackPanel from './FeedbackPanel.svelte';
	import FeedbackPinCard from './FeedbackPinCard.svelte';
	import FeedbackPinDraftBubble from './FeedbackPinDraftBubble.svelte';
	import FeedbackPinVisibilityActions from './FeedbackPinVisibilityActions.svelte';
	import FeedbackPinMarkers from './feedback/FeedbackPinMarkers.svelte';

	const FEEDBACK_UNAVAILABLE =
		'Review todos - this daemon does not serve /api/v1/feedback, so in-app review is unavailable';
	const COMMENT_UNAVAILABLE =
		'Comment pins - this daemon does not serve /api/v1/feedback, so dropping a pin is unavailable';

	/** Pin 88e3abec02a0: worded for an end user, not the agent-facing PRD copy
	 * elsewhere in this file. Shown in the shared ControlExplainer's heading. */
	const FEEDBACK_EXPLAINER_TITLE =
		'Give feedback, ideas and suggestions to the developer, and track them in-app.';

	/** A placed-but-unsaved pin: the bubble the user is typing into.
	 *
	 * `followOn` is set only for a follow-on draft. It carries the parent id
	 * (issue #914 review: kept OUT of `text` on purpose, so editing or
	 * deleting the reviewer's typed text can never lose the link to the
	 * parent) and the display-only label naming that parent. Saving a
	 * follow-on always goes through the dedicated `/follow-on` endpoint,
	 * which generates the real reference server-side; `text` is just the
	 * reviewer's own addition. `page` (from the imported PinDraft) is the
	 * pathname the point/anchor were computed against - see the persistence
	 * effect below for why a draft restored under a different pathname is
	 * dropped rather than reattached. `viewport` is captured at creation time
	 * (main) so a saved pin always carries the viewport it was placed under. */
	let pinDraft:
		| (PinDraft & {
				viewport: { width: number; height: number };
				followOn: { parentId: string; label: string } | null;
		  })
		| null = $state(null);
	/** Viewport fallback for a draft restored from before this field existed. */
	function _currentViewport(): { width: number; height: number } {
		return { width: window.innerWidth, height: window.innerHeight };
	}
	let draftBubble: { focusPinDraftTextarea: () => Promise<void> } | undefined = $state();

	/** Gates the persistence $effect below until onMount's restore has run.
	 * $effect bodies run in declaration order on first flush, before onMount's
	 * callback body executes - so without this gate the effect's first pass
	 * (pinDraft still null) wipes the saved draft before onMount reads it back. */
	let draftHydrated = $state(false);

	/** True from onMount when a parked draft exists but is tagged to a
	 * different page (r3919460207). pinDraft stays null in that case - the
	 * page-mismatch guard from r3919185341 - but that null must NOT be read
	 * by the persistence effect as "nothing to save": the draft is still
	 * sitting in localStorage for its own page and just was not loaded here.
	 * Cleared the moment this page starts its own draft, at which point the
	 * normal null-means-clear lifecycle resumes for real. */
	let foreignDraftParked = $state(false);

	let pathname = $state('/');

	/** Which pin's body is open, and what this viewer has already read. */
	let openPinId: string | null = $state(null);
	let pinSeen: PinSeen = $state({});

	/** Pin 88e3abec02a0: whether comment pin markers are drawn on the canvas
	 * at all. A per-viewer, browser-only preference - a NEW viewer (nothing in
	 * localStorage yet) defaults OFF; an existing viewer's own choice is read
	 * in onMount and always wins over this initial value. */
	let pinsVisible: boolean = $state(false);
	let showHarvestedPins: boolean = $state(true);

	const openCount = $derived(feedbackState.todos.filter((t) => !t.done).length);
	const pagePins = $derived(
		pinsVisible
			? feedbackState.pins.filter((p) => {
					if (!isPinDrawn(p)) return false;
					if (p.author === 'agent' && !uiPrefs.show_agent_pins) return false;
					if (p.status === 'harvested' && !showHarvestedPins) return false;
					return true;
				})
			: []
	);
	const bodyPin = $derived(pagePins.find((p) => p.id === openPinId) ?? null);

	const unavailable = $derived(feedbackState.availability === 'missing');
	const commentPinTitle = $derived.by(() => {
		if (unavailable) return COMMENT_UNAVAILABLE;
		if (feedbackState.availability === 'unknown')
			return 'Comment pins - probing the daemon for /api/v1/feedback';
		const summary = describePinStatusSummary(feedbackState.pins);
		return feedbackState.placementArmed
			? `${summary}. Click anywhere to drop a comment pin (Esc cancels)`
			: `${summary}. Click to drop a comment pin anywhere on the UI`;
	});

	/** Rich explainer bullets (pin 6af63c5e9b7c): the same breakdown as
	 * commentPinTitle, plus - since the hover-count work already landed on
	 * main - the honest statement of what the comment API does NOT track,
	 * stated plainly rather than silently omitted. */
	const commentPinBullets = $derived.by(() => {
		if (unavailable) return [COMMENT_UNAVAILABLE];
		if (feedbackState.availability === 'unknown') {
			return ['Probing the daemon for /api/v1/feedback'];
		}
		return [
			describePinStatusSummary(feedbackState.pins),
			'Delegated / in-progress / queued are not tracked by the comment API yet.'
		];
	});
	const chevronTitle = $derived.by(() => {
		if (unavailable) return FEEDBACK_UNAVAILABLE;
		if (feedbackState.availability === 'unknown')
			return 'Review todos - probing the daemon for /api/v1/feedback (click retries)';
		return `Review todos - ${openCount} open item(s) agents queued for the maintainer's review; check done, pick options, type feedback (auto-saves)`;
	});

	/** Park the unsent draft so a refresh cannot eat it (pin 307e0e84bfbe).
	 * $effect, not a keystroke handler: it must also catch Escape-to-cancel
	 * and the save that clears it. */
	/** Never throws inside the $effect itself - that would tear down the whole
	 * component over a draft that is still perfectly usable in memory.
	 * `persistParkedPinDraft` reports a write failure through `onError` rather
	 * than swallowing it (Sol review r3941617668): `pinDraft` is left
	 * untouched either way, so the text the user typed stays on screen and
	 * editable even when the disk copy could not be written. */
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

	onMount(() => {
		pathname = window.location.pathname;
		// A read failure (storage blocked in a private window, or storage
		// disabled by policy) used to collapse silently into "no draft" -
		// indistinguishable from there simply being none, which is exactly the
		// data-loss condition REFRESH-01 exists to make loud (Sol review
		// r3941617668). readParkedPinDraft reports it through onError before
		// falling back to no draft - there is nothing else safe to restore from
		// a read that failed outright.
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
		// Three GETs (todos, comments, general) behind a chevron nobody has
		// clicked yet: the single biggest block of the boot burst, and the
		// easiest to move (PERF-R6). Deferred, never dropped -- and the
		// chevron's own toggle still calls hydrateFeedback() directly, so a
		// user who clicks during the boot window is not made to wait for it.
		bootScheduler.defer('feedback:hydrate', () => {
			void hydrateFeedback();
		});
		// Same-tab feedback loop (issue #914 review, Wed 2 Sep 2026): hydrate
		// runs once, so without this an agent's PATCH while the reviewer keeps
		// the app open never appears until a full page reload.
		startPinWatch();
		pinSeen = _readSeen();
		pinsVisible = readPinsVisible(window.localStorage);
		showHarvestedPins = parseShowHarvestedPins(window.localStorage.getItem(SHOW_HARVESTED_PINS_KEY));
		// Agent parity: the seen stamp is the one piece of this feature that
		// lives only in the browser, so it needs a programmatic twin.
		_globals().__mdtPinSeen = {
			get: () => ({ ...pinSeen }),
			markSeen: (id: string) => markSeen(id)
		};
		// Same reasoning: the pins-visible preference is also browser-only.
		_globals().__mdtPinsVisible = {
			get: () => pinsVisible,
			set: (value: boolean) => _writePinsVisible(value)
		};
		const flush = () => flushFeedbackSaves();
		window.addEventListener('pagehide', flush);
		return () => {
			stopPinWatch();
			flushFeedbackSaves();
			delete _globals().__mdtPinSeen;
			delete _globals().__mdtPinsVisible;
			window.removeEventListener('pagehide', flush);
		};
	});

	// ----- the unread marker (the maintainer, Wed 2 Sep 2026 17:12) -----------------
	function _globals(): Record<string, unknown> {
		return window as unknown as Record<string, unknown>;
	}

	function _readSeen(): PinSeen {
		try {
			return readPinSeen(window.localStorage);
		} catch {
			return {}; // storage blocked: every update simply reads as unread
		}
	}

	function _writeSeen(next: PinSeen): void {
		pinSeen = next;
		try {
			writePinSeen(window.localStorage, next);
		} catch {
			// storage blocked: the dot returns on reload, which is the safe way round
		}
	}

	/** Agent-facing twin of opening a pin: mark it read AS IT STANDS NOW. */
	function markSeen(pinId: string): void {
		const pin = feedbackState.pins.find((p) => p.id === pinId);
		if (pin !== undefined) _writeSeen(markPinSeen(pinSeen, pin));
	}

	// ----- pin visibility preference (pin 88e3abec02a0) --------------------
	// Fail-fast, deliberately: a blocked or corrupted store here must not be
	// swallowed into a silent OFF (read) or a checkbox that lies about having
	// persisted (write) - both hid a real failure behind a plausible default.
	function _writePinsVisible(next: boolean): void {
		pinsVisible = next;
		writePinsVisible(window.localStorage, next);
	}

	function togglePinsVisible(): void {
		_writePinsVisible(!pinsVisible);
	}

	function toggleAgentPins(): void {
		setShowAgentPins(!uiPrefs.show_agent_pins);
	}

	// ----- pin body -------------------------------------------------------
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

	/** Follow-on opens a draft at the same anchor and position, with the
	 * parent tracked separately from the editable text (issue #914 review:
	 * see the `pinDraft` doc comment for why). `followOnText` here is
	 * display-only labelling, never what gets saved. */
	function startFollowOn(pin: FeedbackPin): void {
		openPinId = null;
		// This page now owns the draft slot for real, overwriting any foreign
		// one on purpose - same rule as handlePlacementClick below (review
		// FeedbackWidget.svelte:225): a foreign draft is superseded, not
		// merged, the moment this page starts writing its own.
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

	/** Thin forwarder to the draft bubble component - kept here (rather than
	 * calling draftBubble?.focusPinDraftTextarea() at each call site) so
	 * startFollowOn/handlePlacementClick read the same either way regardless
	 * of which component now owns the implementation. */
	async function focusPinDraftTextarea(): Promise<void> {
		await draftBubble?.focusPinDraftTextarea();
	}

	// ----- pin placement --------------------------------------------------
	function handlePlacementClick(e: MouseEvent): void {
		const point = pinFromClient(
			e.clientX,
			e.clientY,
			window.innerWidth,
			window.innerHeight
		);
		// The overlay button is the top hit; the anchor is what is under it.
		const under = document
			.elementsFromPoint(e.clientX, e.clientY)
			.find((el) => !el.classList.contains('fb-place-overlay'));
		disarmPinPlacement();
		// This page now owns the draft slot for real, overwriting any foreign
		// one on purpose - the persistence effect's clear guard applies again.
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

<span class="fb-cluster">
	<button
		type="button"
		class="fb-btn"
		class:rb-inert={unavailable}
		class:open={feedbackState.panelOpen}
		disabled={unavailable}
		title={chevronTitle}
		aria-label="Review todos panel"
		aria-expanded={feedbackState.panelOpen}
		onclick={toggleFeedbackPanel}
	>
		<svg width="9" height="6" viewBox="0 0 9 6" aria-hidden="true">
			<path d="M1 1.2 L4.5 4.8 L8 1.2" fill="none" stroke="currentColor" stroke-width="1.4" />
		</svg>
		{#if feedbackState.availability === 'ok' && openCount > 0}
			<span
				class="fb-count"
				title={`${openCount} review todo(s) not yet marked done`}>{openCount}</span
			>
		{/if}
	</button>
	{#snippet pinVisibilityActions()}
		<FeedbackPinVisibilityActions
			{pinsVisible}
			showAgentPins={uiPrefs.show_agent_pins}
			ontoggle={togglePinsVisible}
			onToggleAgentPins={toggleAgentPins}
		/>
	{/snippet}
	<ControlExplainer title={FEEDBACK_EXPLAINER_TITLE} bullets={commentPinBullets} action={pinVisibilityActions}>
		<button
			type="button"
			class="fb-btn"
			class:rb-inert={unavailable}
			class:armed={feedbackState.placementArmed}
			disabled={unavailable}
			title={commentPinTitle}
			aria-label="Drop a comment pin"
			aria-pressed={feedbackState.placementArmed}
			onclick={armPinPlacement}
		>
			<svg width="11" height="10" viewBox="0 0 12 11" aria-hidden="true">
				<path
					d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z"
					fill="none"
					stroke="currentColor"
					stroke-width="1.2"
					stroke-linejoin="round"
				/>
			</svg>
		</button>
	</ControlExplainer>
</span>

<!-- comment pins on this page (#858): color is status, click opens the body -->
<FeedbackPinMarkers pins={pagePins} seen={pinSeen} pathname={pathname} onopen={openPin} />

<!-- pin body: the original text, the agent's reply, its issue, its actions -->
{#if bodyPin !== null}
	<FeedbackPinCard
		pin={bodyPin}
		onclose={closePin}
		onarchive={() => archiveOpenPin(bodyPin)}
		onfollowon={() => startFollowOn(bodyPin)}
		onreply={(text) => addReply(bodyPin.id, text)}
	/>
{/if}

<!-- one-shot placement mode: a full-viewport button so the next click is the pin -->
{#if feedbackState.placementArmed}
	<button
		type="button"
		class="fb-place-overlay"
		aria-label="Click to place the comment pin, Escape to cancel"
		onclick={handlePlacementClick}
	></button>
{/if}

<!-- pin text bubble -->
<FeedbackPinDraftBubble bind:pinDraft bind:this={draftBubble} {pushToast} />

<FeedbackPanel />

<style>
	/* FB-07: left of the vibe meter, which is itself pinned dead-centre. The
	   RIGHT edge is anchored (not the left) so the cluster grows leftwards and
	   its gap to the meter stays fixed when the open-todo count appears or
	   disappears and changes the cluster's width. 92px is the meter's
	   half-width plus a small gap, mirroring the offset this used to sit at on
	   the other side. */
	.fb-cluster {
		position: absolute;
		right: calc(50% + 92px);
		top: 50%;
		transform: translateY(-50%);
		display: inline-flex;
		align-items: center;
		gap: 3px;
		z-index: 2;
	}

	.fb-btn {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		height: 18px;
		padding: 0 4px;
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		cursor: pointer;
		line-height: 1;
	}
	.fb-btn:hover:not(:disabled) {
		color: var(--rb-text);
	}
	.fb-btn.open,
	.fb-btn.armed {
		color: var(--rb-accent);
		border-color: color-mix(in srgb, var(--rb-accent) 55%, var(--rb-border));
	}
	.fb-btn.open svg {
		transform: rotate(180deg);
	}

	.fb-count {
		font-family: var(--rb-font);
		font-size: 9px;
		font-weight: 700;
		color: var(--rb-orange);
	}

	/* The pin markers themselves (.fb-pin and the status palette) live in
	   feedback/FeedbackPinMarkers.svelte, next to the markup they paint. */

	.fb-place-overlay {
		position: fixed;
		inset: 0;
		z-index: 300;
		background: transparent;
		border: none;
		cursor: crosshair;
		padding: 0;
	}

	/* .fb-bubble / .fb-bubble-text / .fb-row-btns / .fb-mini / .fb-close /
	   .fb-hint moved into FeedbackPinDraftBubble.svelte (Thu 3 Sep 2026, pin
	   review v2), next to the markup they paint - same pattern as the pin
	   marker comment above. FeedbackPinCard.svelte's use of the :global()
	   ones still resolves: that stylesheet ships as long as this widget
	   imports the component, regardless of whether pinDraft is currently
	   non-null. */
</style>
