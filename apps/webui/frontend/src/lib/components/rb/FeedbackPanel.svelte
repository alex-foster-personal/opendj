<script lang="ts">
	/**
	 * The draggable review-todo panel (FB-01, FB-02, FB-04): todo list with
	 * done checks / option modal / per-item feedback, and the general note.
	 * Split out of FeedbackWidget.svelte, which owns the topbar cluster and
	 * the comment pins; this component owns everything that renders while
	 * feedbackState.panelOpen is true.
	 *
	 * Panel position and size persist client-side (localStorage, per-viewer
	 * convenience); content lives on /api/v1/feedback.
	 */
	import {
		clampPanelPos,
		clampPanelSize,
		defaultPanelPos,
		defaultPanelSize,
		PANEL_DEFAULT_TOP_PX,
		PANEL_POS_KEY,
		PANEL_SIZE_KEY,
		parsePanelPos,
		parsePanelSize,
		serializePanelPos,
		serializePanelSize,
		startsPanelDrag,
		type DragNode,
		type PanelOpenAnchor,
		type PanelPos,
		type PanelSize
	} from '$lib/rb/feedback';
	import { linkifyAgentNote } from '$lib/rb/feedback';
	import {
		chooseTodoOption,
		feedbackState,
		queueGeneralFeedback,
		queueTodoFeedback,
		setTodoDone,
		toggleFeedbackPanel,
		type FeedbackTodo
	} from '$lib/rb/feedback-store.svelte';

	/** Null until the panel first opens: placement must use a LIVE viewport. */
	let panelPos: PanelPos | null = $state(null);
	let panelSize: PanelSize | null = $state(null);
	let dragging = false;
	let resizing = false;
	let dragOffset: PanelPos = { x: 0, y: 0 };
	let resizeOrigin: PanelPos & PanelSize = { x: 0, y: 0, w: 0, h: 0 };

	/** Per-item text drafts so a PATCH response never clobbers live typing. */
	let todoDrafts: Record<string, string> = $state({});
	let generalDraft: string | null = $state(null);

	let optionModalTodo: FeedbackTodo | null = $state(null);

	const openCount = $derived(feedbackState.todos.filter((t) => !t.done).length);

	function _viewport(): { w: number; h: number } {
		return { w: window.innerWidth, h: window.innerHeight };
	}

	function _livePanelSize(): PanelSize {
		if (panelSize !== null) return panelSize;
		const viewport = _viewport();
		return _readStoredSize() ?? defaultPanelSize(viewport);
	}

	function _readStoredSize(): PanelSize | null {
		try {
			const parsed = parsePanelSize(window.localStorage.getItem(PANEL_SIZE_KEY));
			if (parsed === null) return null;
			return clampPanelSize(parsed, _viewport());
		} catch {
			return null;
		}
	}

	function _persistSize(): void {
		if (panelSize === null) return;
		try {
			window.localStorage.setItem(PANEL_SIZE_KEY, serializePanelSize(panelSize));
		} catch {
			// storage blocked: size will not survive reload
		}
	}

	function _openAnchor(): PanelOpenAnchor {
		const thumbs =
			document.querySelector<HTMLElement>('[data-feedback-thumbs-anchor]') ??
			document.querySelector<HTMLElement>('.topbar-slot-thumbs') ??
			document.querySelector<HTMLElement>('.thumbs-slot');
		const cluster = document.querySelector<HTMLElement>('.fb-cluster');
		const thumbsLeft = thumbs?.getBoundingClientRect().left;
		const clusterRect = cluster?.getBoundingClientRect();
		const anchorLeft = thumbsLeft ?? clusterRect?.left ?? window.innerWidth / 2 - 92;
		const top = Math.round(
			Math.max(
				PANEL_DEFAULT_TOP_PX,
				clusterRect?.bottom ?? thumbs?.getBoundingClientRect().bottom ?? PANEL_DEFAULT_TOP_PX
			)
		);
		return { thumbsLeft: anchorLeft, top };
	}

	// Position and size the panel the first time it opens, against the live viewport.
	$effect(() => {
		if (!feedbackState.panelOpen || panelPos !== null) return;
		const viewport = _viewport();
		panelSize = _readStoredSize() ?? defaultPanelSize(viewport);
		const size = panelSize;
		panelPos = _readStoredPos(size) ?? defaultPanelPos(_openAnchor(), size, viewport);
	});

	function _readStoredPos(size: PanelSize): PanelPos | null {
		try {
			const parsed = parsePanelPos(window.localStorage.getItem(PANEL_POS_KEY));
			if (parsed === null) return null;
			return clampPanelPos(parsed, size, _viewport());
		} catch {
			return null; // storage blocked: default position, panel still works
		}
	}

	function _persistPos(): void {
		if (panelPos === null) return;
		try {
			window.localStorage.setItem(PANEL_POS_KEY, serializePanelPos(panelPos));
		} catch {
			// storage blocked: position simply will not survive reload
		}
	}

	// ----- panel drag -----------------------------------------------------
	function handleDragDown(e: PointerEvent): void {
		if (panelPos === null) return;
		// The close X lives INSIDE this handle. Capturing the pointer here
		// retargets the derived click to the header, so the button's own
		// onclick never runs and the X cannot dismiss the panel.
		if (!startsPanelDrag(e.target as DragNode | null, e.currentTarget as DragNode | null)) return;
		dragging = true;
		dragOffset = { x: e.clientX - panelPos.x, y: e.clientY - panelPos.y };
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handleDragMove(e: PointerEvent): void {
		if (!dragging || panelSize === null) return;
		panelPos = clampPanelPos(
			{ x: e.clientX - dragOffset.x, y: e.clientY - dragOffset.y },
			panelSize,
			_viewport()
		);
	}

	function handleDragUp(e: PointerEvent): void {
		if (!dragging) return;
		dragging = false;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
		_persistPos();
	}

	// ----- inputs ---------------------------------------------------------
	function _todoDraft(todo: FeedbackTodo): string {
		return todoDrafts[todo.id] ?? todo.feedback;
	}

	function handleTodoFeedbackInput(todo: FeedbackTodo, value: string): void {
		todoDrafts[todo.id] = value;
		queueTodoFeedback(todo.id, value);
	}

	function handleGeneralInput(value: string): void {
		generalDraft = value;
		queueGeneralFeedback(value);
	}

	function handleEscape(e: KeyboardEvent): void {
		if (e.key === 'Escape' && optionModalTodo !== null) optionModalTodo = null;
	}

	// ----- panel resize ---------------------------------------------------
	function handleResizeDown(e: PointerEvent): void {
		if (panelPos === null || panelSize === null) return;
		e.stopPropagation();
		resizing = true;
		resizeOrigin = {
			x: e.clientX,
			y: e.clientY,
			w: panelSize.w,
			h: panelSize.h
		};
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handleResizeMove(e: PointerEvent): void {
		if (!resizing || panelPos === null || panelSize === null) return;
		const next = clampPanelSize(
			{
				w: resizeOrigin.w + (e.clientX - resizeOrigin.x),
				h: resizeOrigin.h + (e.clientY - resizeOrigin.y)
			},
			_viewport()
		);
		panelSize = next;
		panelPos = clampPanelPos(panelPos, next, _viewport());
	}

	function handleResizeUp(e: PointerEvent): void {
		if (!resizing) return;
		resizing = false;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
		_persistSize();
		_persistPos();
	}

	const panelLayout = $derived.by(() => {
		if (panelPos === null) return null;
		const size = _livePanelSize();
		return { pos: panelPos, size };
	});
</script>

<svelte:window onkeydown={handleEscape} />

<!-- the review-todo panel -->
{#if feedbackState.panelOpen && panelLayout !== null}
	<div
		class="fb-panel"
		style={`left:${panelLayout.pos.x}px;top:${panelLayout.pos.y}px;width:${panelLayout.size.w}px;height:${panelLayout.size.h}px`}
		role="dialog"
		aria-label="Review todos"
	>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="fb-panel-head"
			title="Drag to move - position is remembered on this machine"
			onpointerdown={handleDragDown}
			onpointermove={handleDragMove}
			onpointerup={handleDragUp}
		>
			<span class="fb-panel-title">REVIEW</span>
			<span
				class="fb-panel-count"
				title={`${openCount} of ${feedbackState.todos.length} review todo(s) still open`}
			>
				{openCount}/{feedbackState.todos.length}
			</span>
			<button
				type="button"
				class="fb-mini fb-close"
				title="Close (panel state is on the server; nothing is lost)"
				aria-label="Close review panel"
				onclick={toggleFeedbackPanel}
			>
				x
			</button>
		</div>

		{#if feedbackState.error !== null}
			<p class="fb-error" title="Last daemon refusal, verbatim">{feedbackState.error}</p>
		{/if}

		<div class="fb-todos">
			{#if feedbackState.todos.length === 0}
				<p class="fb-hint">
					No review todos queued. Agents POST /api/v1/feedback/todos to add one.
				</p>
			{/if}
			{#each feedbackState.todos as todo (todo.id)}
				<div class="fb-todo" class:done={todo.done}>
					<label class="fb-todo-row">
						<input
							type="checkbox"
							checked={todo.done}
							title="Mark reviewed (saves immediately)"
							onchange={(e) => setTodoDone(todo.id, (e.currentTarget as HTMLInputElement).checked)}
						/>
						<span class="fb-todo-title" title={todo.detail ?? todo.title}>{todo.title}</span>
					</label>
					{#if todo.detail}
						<p class="fb-todo-detail">
							{#each linkifyAgentNote(todo.detail) as segment, i (i)}
								{#if segment.type === 'link'}
									<a
										href={segment.value}
										target="_blank"
										rel="noreferrer noopener"
										onclick={(e) => e.stopPropagation()}>{segment.value}</a
									>
								{:else}{segment.value}{/if}
							{/each}
						</p>
					{/if}
					{#if todo.options.length > 0}
						<button
							type="button"
							class="fb-mini"
							title="Pick one of the options the agent offered"
							onclick={() => (optionModalTodo = todo)}
						>
							{todo.chosen_option ?? 'choose...'}
						</button>
					{/if}
					<input
						class="fb-todo-input"
						type="text"
						placeholder="feedback (auto-saves)"
						title="Freeform feedback for this item - auto-saves debounced"
						value={_todoDraft(todo)}
						oninput={(e) =>
							handleTodoFeedbackInput(todo, (e.currentTarget as HTMLInputElement).value)}
					/>
				</div>
			{/each}
		</div>

		<div class="fb-general">
			<textarea
				class="fb-general-text"
				rows="2"
				placeholder="general feedback (auto-saves, kept until harvested)"
				title="General feedback - auto-saves debounced, stamped with the build it was given on, never auto-deleted"
				value={generalDraft ?? feedbackState.general?.text ?? ''}
				oninput={(e) => handleGeneralInput((e.currentTarget as HTMLTextAreaElement).value)}
			></textarea>
		</div>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="fb-panel-resize"
			title="Drag to resize - size is remembered on this machine"
			onpointerdown={handleResizeDown}
			onpointermove={handleResizeMove}
			onpointerup={handleResizeUp}
		></div>
	</div>
{/if}

<!-- option choice modal -->
{#if optionModalTodo !== null}
	<div class="fb-modal-backdrop" role="presentation">
		<div class="fb-modal" role="dialog" aria-label="Choose an option">
			<p class="fb-panel-title">{optionModalTodo.title}</p>
			{#each optionModalTodo.options as option (option)}
				<button
					type="button"
					class="fb-option"
					class:on={optionModalTodo.chosen_option === option}
					onclick={async () => {
						await chooseTodoOption(optionModalTodo!.id, option);
						optionModalTodo = null;
					}}
				>
					{option}
				</button>
			{/each}
			<button type="button" class="fb-mini" onclick={() => (optionModalTodo = null)}>Cancel</button>
		</div>
	</div>
{/if}

<style>
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

	.fb-panel {
		position: fixed;
		z-index: 120;
		display: flex;
		flex-direction: column;
		box-sizing: border-box;
		padding: 2px;
		border-radius: 4px;
		background: linear-gradient(
			120deg,
			#ff6b6b,
			#ffa94d,
			#ffd43b,
			#69db7c,
			#4dabf7,
			#9775fa,
			#ff6b6b
		);
		background-size: 320% 320%;
		animation: fb-panel-rainbow 6s linear infinite;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
	}

	.fb-panel::before {
		content: '';
		position: absolute;
		inset: 2px;
		border-radius: 2px;
		background: var(--rb-well, #0a0c0f);
		pointer-events: none;
		z-index: 0;
	}

	.fb-panel > :not(.fb-panel-resize) {
		position: relative;
		z-index: 1;
	}

	@keyframes fb-panel-rainbow {
		0% {
			background-position: 0% 50%;
		}
		100% {
			background-position: 100% 50%;
		}
	}

	.fb-panel-resize {
		position: absolute;
		right: 0;
		bottom: 0;
		z-index: 2;
		width: 12px;
		height: 12px;
		cursor: nwse-resize;
		touch-action: none;
		background: linear-gradient(135deg, transparent 50%, var(--rb-text-dim) 50%);
		opacity: 0.65;
		border-bottom-right-radius: 2px;
	}
	.fb-panel-resize:hover {
		opacity: 1;
	}

	.fb-panel-head {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 5px 6px;
		background: var(--rb-well, #0a0c0f);
		border-bottom: 1px solid var(--rb-border);
		cursor: grab;
		touch-action: none;
		user-select: none;
	}
	.fb-panel-head:active {
		cursor: grabbing;
	}

	.fb-panel-title {
		font-family: var(--rb-font-brand);
		font-weight: 650;
		letter-spacing: 0.06em;
		font-size: 9px;
	}

	.fb-panel-count {
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}

	.fb-close {
		margin-left: auto;
	}

	.fb-error {
		margin: 4px 6px 0;
		color: var(--rb-red, #d24b4b);
	}

	.fb-todos {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		background: var(--rb-well, #0a0c0f);
		padding: 4px 6px;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.fb-todo {
		display: flex;
		flex-direction: column;
		gap: 3px;
		padding-bottom: 5px;
		border-bottom: 1px dotted var(--rb-border);
	}
	.fb-todo.done .fb-todo-title {
		text-decoration: line-through;
		color: var(--rb-text-dim);
	}

	.fb-todo-row {
		display: flex;
		align-items: center;
		gap: 5px;
		cursor: pointer;
	}
	.fb-todo-row input {
		margin: 0;
	}

	.fb-todo-title {
		flex: 1;
		min-width: 0;
		white-space: normal;
		overflow-wrap: anywhere;
		line-height: 1.25;
	}

	.fb-todo-detail {
		margin: 0;
		color: var(--rb-text-dim);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		line-height: 1.35;
		user-select: text;
	}

	.fb-hint {
		margin: 2px 0 0;
		color: var(--rb-text-dim);
	}

	.fb-general {
		padding: 5px 6px 6px;
		background: var(--rb-well, #0a0c0f);
		border-top: 1px solid var(--rb-border);
	}

	.fb-general-text,
	.fb-todo-input {
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

	.fb-general-text::placeholder,
	.fb-todo-input::placeholder {
		color: var(--rb-text-dim);
		opacity: 0.55;
	}

	.fb-modal-backdrop {
		position: fixed;
		inset: 0;
		z-index: 320;
		background: rgba(0, 0, 0, 0.45);
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.fb-modal {
		display: flex;
		flex-direction: column;
		gap: 5px;
		width: 220px;
		padding: 8px 9px;
		background: var(--rb-well, #0a0c0f);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
	}

	.fb-option {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		padding: 4px 6px;
		cursor: pointer;
		text-align: left;
	}
	.fb-option:hover {
		border-color: color-mix(in srgb, var(--rb-accent) 55%, var(--rb-border));
	}
	.fb-option.on {
		color: var(--rb-accent);
		border-color: color-mix(in srgb, var(--rb-accent) 55%, var(--rb-border));
	}
	@media (prefers-reduced-motion: reduce) {
		.fb-panel {
			animation: none;
		}
	}
	:global([data-reduce-motion]) .fb-panel {
		animation: none;
	}
</style>
