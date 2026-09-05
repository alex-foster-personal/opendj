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
	 * comment icon render inert with the standard PARITY-TODO tooltip, never
	 * a broken panel.
	 */
	import { onMount, tick } from 'svelte';
	import {
		describePinStatusSummary,
		describeAnchor,
		followOnText,
		isPinDrawn,
		markPinSeen,
		parsePinSeen,
		pinFromClient,
		pinStyle,
		serializePinSeen,
		PIN_SEEN_KEY,
		type PinPoint,
		type PinSeen
	} from '$lib/rb/feedback';
	import { readPinsVisible, writePinsVisible } from '$lib/rb/feedback-pin-visibility';
	import {
		addPin,
		archivePin,
		armPinPlacement,
		disarmPinPlacement,
		feedbackState,
		flushFeedbackSaves,
		hydrateFeedback,
		startPinWatch,
		stopPinWatch,
		submitFollowOn,
		toggleFeedbackPanel,
		type FeedbackPin
	} from '$lib/rb/feedback-store.svelte';
	import { readShellBuild } from '$lib/rb/build-identity';
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import ControlExplainer from './deck/ControlExplainer.svelte';
	import FeedbackPanel from './FeedbackPanel.svelte';
	import FeedbackPinCard from './FeedbackPinCard.svelte';
	import FeedbackPinVisibilityActions from './FeedbackPinVisibilityActions.svelte';
	import FeedbackPinMarkers from './feedback/FeedbackPinMarkers.svelte';

	const INERT_TITLE = 'not implemented - see PARITY-TODO';

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
	 * reviewer's own addition. */
	let pinDraft: {
		point: PinPoint;
		anchor: string | null;
		viewport: { width: number; height: number };
		text: string;
		followOn: { parentId: string; label: string } | null;
	} | null = $state(null);
	let pinDraftTextarea: HTMLTextAreaElement | null = $state(null);

	let pathname = $state('/');

	/** True while a savePinDraft() POST is in flight. Guards the Save button:
	 * without it, a double-click reaches submitFollowOn/addPin twice before
	 * `pinDraft` clears, and the dedicated follow-on endpoint mints a fresh
	 * child id per request, so one intended follow-on becomes two pins
	 * (issue #914 review, Wed 3 Sep 2026). */
	let savingPin = $state(false);

	/** Which pin's body is open, and what this viewer has already read. */
	let openPinId: string | null = $state(null);
	let pinSeen: PinSeen = $state({});

	/** Pin 88e3abec02a0: whether comment pin markers are drawn on the canvas
	 * at all. A per-viewer, browser-only preference - a NEW viewer (nothing in
	 * localStorage yet) defaults OFF; an existing viewer's own choice is read
	 * in onMount and always wins over this initial value. */
	let pinsVisible: boolean = $state(false);

	const openCount = $derived(feedbackState.todos.filter((t) => !t.done).length);
	const pagePins = $derived(
		pinsVisible ? feedbackState.pins.filter((p) => p.page === pathname && isPinDrawn(p)) : []
	);
	const bodyPin = $derived(pagePins.find((p) => p.id === openPinId) ?? null);

	const unavailable = $derived(feedbackState.availability === 'missing');
	const commentPinTitle = $derived.by(() => {
		if (unavailable) return INERT_TITLE;
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
		if (unavailable) return [INERT_TITLE];
		if (feedbackState.availability === 'unknown') {
			return ['Probing the daemon for /api/v1/feedback'];
		}
		return [
			describePinStatusSummary(feedbackState.pins),
			'Delegated / in-progress / queued are not tracked by the comment API yet.'
		];
	});
	const chevronTitle = $derived.by(() => {
		if (unavailable) return INERT_TITLE;
		if (feedbackState.availability === 'unknown')
			return 'Review todos - probing the daemon for /api/v1/feedback (click retries)';
		return `Review todos - ${openCount} open item(s) agents queued for the maintainer's review; check done, pick options, type feedback (auto-saves)`;
	});

	onMount(() => {
		pathname = window.location.pathname;
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
			return parsePinSeen(window.localStorage.getItem(PIN_SEEN_KEY));
		} catch {
			return {}; // storage blocked: every update simply reads as unread
		}
	}

	function _writeSeen(next: PinSeen): void {
		pinSeen = next;
		try {
			window.localStorage.setItem(PIN_SEEN_KEY, serializePinSeen(next));
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

	// ----- pin body -------------------------------------------------------
	function openPin(pin: FeedbackPin): void {
		openPinId = pin.id;
		_writeSeen(markPinSeen(pinSeen, pin));
	}

	function closePin(): void {
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
		pinDraft = {
			point: { x_pct: pin.x_pct, y_pct: pin.y_pct },
			anchor: pin.anchor,
			viewport: { width: window.innerWidth, height: window.innerHeight },
			text: '',
			followOn: { parentId: pin.id, label: followOnText(pin).trim() }
		};
		void focusPinDraftTextarea();
	}

	async function focusPinDraftTextarea(): Promise<void> {
		await tick();
		if (pinDraftTextarea === null) {
			throw new Error('pin draft textarea did not render before focus');
		}
		pinDraftTextarea.focus();
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
		pinDraft = {
			point,
			anchor: describeAnchor(under ?? null),
			viewport: { width: window.innerWidth, height: window.innerHeight },
			text: '',
			followOn: null
		};
		void focusPinDraftTextarea();
	}

	async function savePinDraft(): Promise<void> {
		if (pinDraft === null || savingPin) return;
		savingPin = true;
		try {
			// Textarea and Cancel stay enabled during the await below, so the
			// reviewer can edit, cancel, or start a replacement draft before this
			// save resolves; only clear `pinDraft` if it is still THIS draft.
			const submitted = pinDraft;
			const text = submitted.text.trim();
			if (submitted.followOn !== null) {
				// Always through the dedicated endpoint: the parent reference is
				// server-generated from submitted.followOn.parentId, independent of
				// whatever is (or is not) left in `text`.
				const saved = await submitFollowOn(submitted.followOn.parentId, text);
				if (saved && pinDraft === submitted) pinDraft = null;
				return;
			}
			if (text === '') return;
			const saved = await addPin({
				x_pct: submitted.point.x_pct,
				y_pct: submitted.point.y_pct,
				anchor: submitted.anchor,
				page: pathname,
				text,
				ui: pinUiKind(),
				viewport_width: submitted.viewport.width,
				viewport_height: submitted.viewport.height
			});
			if (saved && pinDraft === submitted) pinDraft = null;
		} finally {
			savingPin = false;
		}
	}

	function pinUiKind(): 'chrome-loop' | 'packaged-app' {
		return readShellBuild().kind === 'absent' ? 'chrome-loop' : 'packaged-app';
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
		<FeedbackPinVisibilityActions {pinsVisible} ontoggle={togglePinsVisible} />
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
<FeedbackPinMarkers pins={pagePins} seen={pinSeen} onopen={openPin} />

<!-- pin body: the original text, the agent's reply, its issue, its actions -->
{#if bodyPin !== null}
	<FeedbackPinCard
		pin={bodyPin}
		onclose={closePin}
		onarchive={() => archiveOpenPin(bodyPin)}
		onfollowon={() => startFollowOn(bodyPin)}
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
{#if pinDraft !== null}
	<div class="fb-bubble" style={pinStyle(pinDraft.point)} role="dialog" aria-label="New comment pin">
		{#if pinDraft.followOn !== null}
			<p class="fb-hint" title="Sent through the dedicated follow-on endpoint; the reference below is generated server-side">{pinDraft.followOn.label}</p>
		{/if}
		<textarea
			class="fb-bubble-text"
			rows="3"
			placeholder="What is wrong / right here?"
			bind:this={pinDraftTextarea}
			bind:value={pinDraft.text}
		></textarea>
		{#if pinDraft.anchor !== null}
			<p class="fb-hint" title="Best-effort nearest stable element under the click">near {pinDraft.anchor}</p>
		{/if}
		<div class="fb-row-btns">
			<button
				type="button"
				class="fb-mini"
				onclick={savePinDraft}
				disabled={savingPin || (pinDraft.followOn === null && pinDraft.text.trim() === '')}
			>
				Save pin
			</button>
			<button type="button" class="fb-mini" onclick={() => (pinDraft = null)}>Cancel</button>
		</div>
	</div>
{/if}

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

	.fb-bubble {
		position: fixed;
		z-index: 310;
		width: 200px;
		padding: 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
	}

	.fb-bubble-text {
		width: 100%;
		box-sizing: border-box;
		background: var(--rb-inset);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		padding: 3px 4px;
	}

	:global(.fb-row-btns) {
		display: flex;
		gap: 4px;
		margin-top: 4px;
	}

	:global(.fb-mini) {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 9px;
		padding: 2px 6px;
		line-height: 1.2;
		cursor: pointer;
	}
	:global(.fb-mini:hover:not(:disabled)) {
		color: var(--rb-text);
	}
	:global(.fb-close) {
		margin-left: auto;
		min-width: 20px;
	}

	:global(.fb-hint) {
		margin: 2px 0 0;
		color: var(--rb-text-dim);
	}

</style>
