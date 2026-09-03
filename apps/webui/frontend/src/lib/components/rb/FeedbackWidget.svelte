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
	import { onMount } from 'svelte';
	import {
		describeAnchor,
		followOnText,
		isPinDrawn,
		isPinUnread,
		markPinSeen,
		parsePinSeen,
		pinFromClient,
		pinBodyStyle,
		pinIsDone,
		pinStatus,
		pinStyle,
		serializePinSeen,
		PIN_SEEN_KEY,
		type PinPoint,
		type PinSeen
	} from '$lib/rb/feedback';
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
	import { bootScheduler } from '$lib/rb/boot-scheduler';
	import FeedbackPanel from './FeedbackPanel.svelte';

	const INERT_TITLE = 'not implemented - see PARITY-TODO';

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
		text: string;
		followOn: { parentId: string; label: string } | null;
	} | null = $state(null);

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

	const openCount = $derived(feedbackState.todos.filter((t) => !t.done).length);
	const pagePins = $derived(
		feedbackState.pins.filter((p) => p.page === pathname && isPinDrawn(p))
	);
	const bodyPin = $derived(pagePins.find((p) => p.id === openPinId) ?? null);

	const unavailable = $derived(feedbackState.availability === 'missing');
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
		// Agent parity: the seen stamp is the one piece of this feature that
		// lives only in the browser, so it needs a programmatic twin.
		_globals().__mdtPinSeen = {
			get: () => ({ ...pinSeen }),
			markSeen: (id: string) => markSeen(id)
		};
		const flush = () => flushFeedbackSaves();
		window.addEventListener('pagehide', flush);
		return () => {
			stopPinWatch();
			flushFeedbackSaves();
			delete _globals().__mdtPinSeen;
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
			text: '',
			followOn: { parentId: pin.id, label: followOnText(pin).trim() }
		};
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
			text: '',
			followOn: null
		};
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
				text
			});
			if (saved && pinDraft === submitted) pinDraft = null;
		} finally {
			savingPin = false;
		}
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
	<button
		type="button"
		class="fb-btn"
		class:rb-inert={unavailable}
		class:armed={feedbackState.placementArmed}
		disabled={unavailable}
		title={unavailable
			? INERT_TITLE
			: feedbackState.placementArmed
				? 'Click anywhere to drop a comment pin (Esc cancels)'
				: 'Drop a comment anywhere on the UI - arms one placement click'}
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
</span>

<!-- comment pins on this page (#858): colour is status, click opens the body -->
{#each pagePins as pin (pin.id)}
	{@const status = pinStatus(pin)}
	<button
		type="button"
		class="fb-pin fb-{status}"
		class:fb-unread={isPinUnread(pin, pinSeen)}
		style={pinStyle(pin)}
		title={`${pin.text} - ${pin.created_at}${pin.anchor ? ` (near ${pin.anchor})` : ''}`}
		aria-label={`Comment pin (${status}) - open`}
		onclick={() => openPin(pin)}
	>
		<svg class="fb-mark" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
			<path d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z" />
		</svg>
	</button>
{/each}

<!-- pin body: the original text, the agent's reply, its issue, its actions -->
{#if bodyPin !== null}
	<div class="fb-pin-body" style={pinBodyStyle(bodyPin)} role="dialog" aria-label="Comment pin">
		<p class="fb-hint">{pinStatus(bodyPin)} - {bodyPin.created_at}</p>
		<p class="fb-body-text">{bodyPin.text}</p>
		{#if bodyPin.agent_note}
			<p class="fb-note" title="What an agent did about this pin">{bodyPin.agent_note}</p>
		{/if}
		{#if bodyPin.issue_url}
			<a
				class="fb-issue-link"
				href={bodyPin.issue_url}
				target="_blank"
				rel="noreferrer noopener"
				title="Opens in the default browser">{bodyPin.issue_url}</a
			>
		{/if}
		<div class="fb-row-btns">
			{#if pinIsDone(bodyPin)}
				<button
					type="button"
					class="fb-mini"
					title="Move this pin into the archive file, with its history"
					onclick={() => void archiveOpenPin(bodyPin)}>Archive</button
				>
				<button
					type="button"
					class="fb-mini"
					title="Open a new pin here, referencing this one"
					onclick={() => startFollowOn(bodyPin)}>Follow-on</button
				>
			{/if}
			<button type="button" class="fb-mini" onclick={closePin}>Close</button>
		</div>
	</div>
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

	/* #858 pin lifecycle. open = amber (unchanged), issued = amber + link
	   glyph, fixed = green OUTLINE, merged = SOLID green, archived = not
	   drawn at all (filtered out of pagePins, never merely hidden). */
	.fb-pin {
		position: fixed;
		z-index: 80;
		transform: translate(-50%, -50%);
		color: var(--rb-orange);
		cursor: pointer;
		background: none;
		border: none;
		padding: 0;
		line-height: 0;
	}
	.fb-mark path {
		fill: currentColor;
	}
	.fb-fixed,
	.fb-merged {
		color: var(--rb-green);
	}
	.fb-fixed .fb-mark path {
		fill: none;
		stroke: currentColor;
		stroke-width: 1.3;
		stroke-linejoin: round;
	}
	.fb-merged .fb-mark path {
		fill: currentColor;
	}
	/* issued: amber still, plus the link mark that says it has been filed */
	.fb-issued::before {
		content: '';
		position: absolute;
		left: 7px;
		bottom: 0;
		width: 6px;
		height: 3px;
		border: 1px solid currentColor;
		border-radius: 2px;
	}
	/* the unread dot: an agent has written to this pin since the maintainer read it */
	.fb-unread::after {
		content: '';
		position: absolute;
		top: -2px;
		right: -3px;
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: var(--rb-accent);
	}

	.fb-pin-body {
		position: fixed;
		z-index: 310;
		width: 240px;
		max-height: 320px;
		overflow-y: auto;
		padding: 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-size: 10px;
	}
	.fb-body-text {
		margin: 2px 0 0;
	}
	.fb-note {
		margin: 4px 0 0;
		padding-top: 4px;
		border-top: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
	}
	.fb-issue-link {
		display: block;
		margin-top: 4px;
		color: var(--rb-accent);
		word-break: break-all;
	}

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

	.fb-row-btns {
		display: flex;
		gap: 4px;
		margin-top: 4px;
	}

	.fb-mini {
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
	.fb-mini:hover:not(:disabled) {
		color: var(--rb-text);
	}

	.fb-hint {
		margin: 2px 0 0;
		color: var(--rb-text-dim);
	}
</style>
