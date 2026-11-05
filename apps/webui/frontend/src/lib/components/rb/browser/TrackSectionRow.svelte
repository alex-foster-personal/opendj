<script lang="ts">
	import { DEFAULT_SECTION_TITLE, type SectionHeader } from '$lib/library/playlist-sections';
	import SectionContextMenu from './SectionContextMenu.svelte';

	/**
	 * One divider in the track table (R-SEC-1, R-SEC-3, D46). A single click on
	 * the title does nothing: double-click or the right-click menu renames, and
	 * the remove control shows only on hover or keyboard focus. Removing a divider leaves its
	 * tracks where they are.
	 */
	let {
		header,
		colCount,
		rowHeight,
		editable,
		movable,
		renaming,
		dragged,
		transform,
		onstartrename,
		onrename,
		oncancelrename,
		onremove,
		ontoggle,
		oninsertabove,
		ondragstart,
		ondragend
	}: {
		header: SectionHeader;
		colCount: number;
		rowHeight: number;
		editable: boolean;
		movable: boolean;
		renaming: boolean;
		dragged: boolean;
		transform: string | undefined;
		onstartrename: () => void;
		onrename: (title: string) => void;
		oncancelrename: () => void;
		onremove: () => void;
		ontoggle: () => void;
		oninsertabove: () => void;
		ondragstart: (event: DragEvent) => void;
		ondragend: () => void;
	} = $props();

	/** Selected at once, and again a frame later if the closing right-click menu
	 * took focus back in between (D46). */
	function selectOnMount(input: HTMLInputElement): void {
		cancelled = false;
		input.focus();
		input.select();
		requestAnimationFrame(() => {
			if (document.activeElement === input) return;
			input.focus();
			input.select();
		});
	}

	let cancelled = false;
	let menu = $state<SectionContextMenu | undefined>(undefined);

	function commit(event: FocusEvent): void {
		if (cancelled) return;
		const title = (event.currentTarget as HTMLInputElement).value.trim();
		onrename(title === '' ? DEFAULT_SECTION_TITLE : title);
	}

	function onInputKey(event: KeyboardEvent): void {
		event.stopPropagation();
		if (event.key === 'Enter') {
			(event.currentTarget as HTMLInputElement).blur();
		} else if (event.key === 'Escape') {
			cancelled = true;
			oncancelrename();
		}
	}

	function onTitleKey(event: KeyboardEvent): void {
		if (editable && (event.key === 'Enter' || event.key === 'F2')) {
			event.preventDefault();
			event.stopPropagation();
			onstartrename();
		}
	}
</script>

<tr
	class="tt-section"
	class:tt-dragged={dragged}
	data-testid="section-row"
	data-section-id={header.section.id}
	draggable={movable && !renaming}
	style:height={`${rowHeight}px`}
	style:transform
	oncontextmenu={(event) => menu?.open(event)}
	{ondragstart}
	{ondragend}
>
	<td colspan={colCount}>
		<div class="tt-section-line">
			<button
				type="button"
				class="tt-section-chevron"
				class:open={!header.collapsed}
				aria-expanded={!header.collapsed}
				aria-label={`${header.collapsed ? 'Expand' : 'Collapse'} ${header.section.title}`}
				onclick={ontoggle}
			>
				<svg viewBox="0 0 8 8" width="8" height="8" aria-hidden="true"><path d="M2 1l4 3-4 3z" fill="currentColor" /></svg>
			</button>
			{#if renaming}
				<input
					class="tt-section-input"
					data-testid="section-title-input"
					aria-label="Section title"
					value={header.section.title}
					use:selectOnMount
					onblur={commit}
					onkeydown={onInputKey}
				/>
			{:else}
				<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
				<span
					class="tt-section-title"
					data-testid="section-title"
					role="heading"
					aria-level="3"
					tabindex={editable ? 0 : -1}
					ondblclick={() => {
						if (editable) onstartrename();
					}}
					onkeydown={onTitleKey}
				>{header.section.title}</span>
			{/if}
			{#if header.collapsed}
				<span class="tt-section-count">{header.trackCount}</span>
			{/if}
			{#if editable}
				<button
					type="button"
					class="tt-section-remove"
					data-testid="section-remove"
					title="Remove divider. Its tracks stay."
					aria-label={`Remove divider ${header.section.title}`}
					onclick={onremove}
				>
					<svg viewBox="0 0 8 8" width="8" height="8" aria-hidden="true"><path d="M1 1l6 6M7 1l-6 6" stroke="currentColor" stroke-width="1.2" fill="none" /></svg>
				</button>
			{/if}
		</div>
		<SectionContextMenu
			bind:this={menu}
			{editable}
			onrename={onstartrename}
			oninsertabove={() => oninsertabove()}
			onremove={onremove}
		/>
	</td>
</tr>

<style>
	.tt-section {
		transition: transform var(--tt-gap-ms, 100ms) ease-out;
	}
	.tt-section.tt-dragged {
		opacity: 0.4;
	}
	.tt-section td {
		padding: 0;
		overflow: hidden;
		border: 0;
	}
	/* The slot height is fixed for the virtual window, so "more space above than
	 * below" (D6) comes from sitting the line on the slot's bottom edge. */
	.tt-section-line {
		display: flex;
		align-items: flex-end;
		gap: 4px;
		height: 100%;
		padding: 0 8px 1px 6px;
		box-sizing: border-box;
	}
	.tt-section-title {
		color: var(--rb-accent);
		font-size: 11px;
		font-weight: 700;
		line-height: 13px;
		white-space: nowrap;
		cursor: default;
		user-select: none;
	}
	.tt-section-input {
		min-width: 12ch;
		height: 15px;
		padding: 0 2px;
		color: var(--rb-accent);
		font: inherit;
		font-size: 11px;
		font-weight: 700;
		background: transparent;
		border: 1px solid var(--rb-accent);
		border-radius: 2px;
		outline: 0;
	}
	.tt-section-count {
		color: var(--rb-text-dim);
		font-size: 10px;
		line-height: 13px;
	}
	.tt-section-chevron,
	.tt-section-remove {
		display: inline-flex;
		align-items: center;
		height: 13px;
		padding: 0;
		color: var(--rb-text-dim);
		font-size: 11px;
		line-height: 13px;
		background: none;
		border: 0;
		cursor: pointer;
	}
	.tt-section-chevron.open {
		transform: rotate(90deg);
	}
	.tt-section-remove {
		margin-left: auto;
		opacity: 0;
	}
	.tt-section:hover .tt-section-remove,
	.tt-section:focus-within .tt-section-remove {
		opacity: 1;
	}
	.tt-section-remove:hover {
		color: var(--rb-accent);
	}
</style>
