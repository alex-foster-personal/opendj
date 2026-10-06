<script lang="ts">
	/**
	 * The draggable review-todo panel (FB-01, FB-02, FB-04): todo list with
	 * done checks / option modal / per-item feedback, and the general note.
	 * Split out of FeedbackWidget.svelte, which owns the topbar cluster and
	 * the comment pins; this component owns everything that renders while
	 * feedbackState.panelOpen is true.
	 *
	 * Only the panel POSITION persists client-side (localStorage, per-viewer
	 * convenience); content lives on /api/v1/feedback.
	 */
	import {
		clampPanelPos,
		PANEL_POS_KEY,
		parsePanelPos,
		serializePanelPos,
		startsPanelDrag,
		type DragNode,
		type PanelPos
	} from '$lib/rb/feedback';
	import {
		chooseTodoOption,
		feedbackState,
		queueGeneralFeedback,
		queueTodoFeedback,
		setTodoDone,
		toggleFeedbackPanel,
		type FeedbackTodo
	} from '$lib/rb/feedback-store.svelte';

	const PANEL_W = 272;
	const PANEL_H = 300;

	/** Null until the panel first opens: the position must be computed against
	 * a LIVE viewport. Computing it at mount clamps to {0,0} when the tab
	 * mounts hidden or before layout (innerWidth reads 0). */
	let panelPos: PanelPos | null = $state(null);
	let dragging = false;
	let dragOffset: PanelPos = { x: 0, y: 0 };

	/** Per-item text drafts so a PATCH response never clobbers live typing. */
	let todoDrafts: Record<string, string> = $state({});
	let generalDraft: string | null = $state(null);

	let optionModalTodo: FeedbackTodo | null = $state(null);

	const openCount = $derived(feedbackState.todos.filter((t) => !t.done).length);

	// Position the panel the first time it opens, against the live viewport.
	$effect(() => {
		if (!feedbackState.panelOpen || panelPos !== null) return;
		panelPos =
			_readStoredPos() ??
			clampPanelPos(
				{ x: window.innerWidth / 2 - PANEL_W / 2, y: 34 },
				{ w: PANEL_W, h: PANEL_H },
				{ w: window.innerWidth, h: window.innerHeight }
			);
	});

	function _readStoredPos(): PanelPos | null {
		try {
			const parsed = parsePanelPos(window.localStorage.getItem(PANEL_POS_KEY));
			if (parsed === null) return null;
			return clampPanelPos(
				parsed,
				{ w: PANEL_W, h: PANEL_H },
				{ w: window.innerWidth, h: window.innerHeight }
			);
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
		if (!startsPanelDrag(e.target as DragNode | null, e.currentTarget as DragNode | null))
			return;
		dragging = true;
		dragOffset = { x: e.clientX - panelPos.x, y: e.clientY - panelPos.y };
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handleDragMove(e: PointerEvent): void {
		if (!dragging) return;
		panelPos = clampPanelPos(
			{ x: e.clientX - dragOffset.x, y: e.clientY - dragOffset.y },
			{ w: PANEL_W, h: PANEL_H },
			{ w: window.innerWidth, h: window.innerHeight }
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
</script>

<svelte:window onkeydown={handleEscape} />

<!-- the review-todo panel -->
{#if feedbackState.panelOpen && panelPos !== null}
	<div
		class="fb-panel"
		style={`left:${panelPos.x}px;top:${panelPos.y}px;width:${PANEL_W}px`}
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
				<p class="fb-hint">No review todos queued. Agents POST /api/v1/feedback/todos to add one.</p>
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
						<p class="fb-todo-detail">{todo.detail}</p>
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
		max-height: 46vh;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
	}

	.fb-panel-head {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 5px 6px;
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
		overflow-y: auto;
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
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.fb-todo-detail {
		margin: 0;
		color: var(--rb-text-dim);
	}

	.fb-hint {
		margin: 2px 0 0;
		color: var(--rb-text-dim);
	}

	.fb-general {
		padding: 5px 6px 6px;
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
		background: #0a0c0f;
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
</style>
