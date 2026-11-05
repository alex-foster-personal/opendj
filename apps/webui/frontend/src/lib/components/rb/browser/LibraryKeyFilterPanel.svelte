<script lang="ts">
	import {
		compatibleLibraryKeys,
		libraryKeySegmentFill,
		libraryKeySegmentOpacity,
		libraryKeyWheelAriaLabel,
		libraryKeyWheelLabelLines,
		libraryKeyWheelSegments,
		LIBRARY_KEY_WHEEL_SIZE,
		resetLibraryKeySelection,
		toggleLibraryKeySelection
	} from '$lib/rb/library-key-filter';
	import { displayKey, type KeyNotation } from '$lib/rb/key-notation';

	let {
		referenceKey,
		keys,
		notation,
		save,
		onClose
	}: {
		/** Canonical Camelot code of the selected row, else the playing deck. */
		referenceKey: string | null;
		/** The persisted selection (ui-prefs library_key_filter). */
		keys: readonly string[];
		/** ui-prefs key_notation: every label follows it. */
		notation: KeyNotation;
		/** Persists a new selection (prefs setLibraryKeyFilter). */
		save: (keys: readonly string[]) => Promise<void>;
		onClose: () => void;
	} = $props();

	const segments = libraryKeyWheelSegments();
	const CENTER = LIBRARY_KEY_WHEEL_SIZE / 2;
	let saveError = $state<string | null>(null);
	let pendingKeys = $state<readonly string[] | null>(null);
	let keysRequest = 0;

	const shownKeys = $derived<readonly string[]>(pendingKeys ?? keys);
	const anySelected = $derived(shownKeys.length > 0);
	const compatibleKeys = $derived(referenceKey === null ? [] : compatibleLibraryKeys(referenceKey));
	const referenceText = $derived(
		referenceKey === null ? null : (displayKey(referenceKey, notation)?.text ?? referenceKey)
	);
	const compatibleTitle = $derived(
		referenceKey === null
			? 'Select a track or play a deck with a key to use Compatible'
			: `Select ${compatibleKeys.map((c) => displayKey(c, notation)?.text ?? c).join(', ')}`
	);

	function _commit(next: readonly string[]): void {
		const request = ++keysRequest;
		saveError = null;
		pendingKeys = next;
		save(next)
			.catch((err: unknown) => {
				if (request !== keysRequest) return;
				const detail = err instanceof Error ? err.message : String(err);
				saveError = `Could not save key filter: ${detail}`;
			})
			.finally(() => {
				if (request === keysRequest) pendingKeys = null;
			});
	}

	function _toggle(code: string): void {
		_commit(toggleLibraryKeySelection(shownKeys, code));
	}

	function _compatible(): void {
		if (referenceKey === null) return;
		_commit(compatibleKeys);
	}

	function _reset(): void {
		if (!anySelected) return;
		_commit(resetLibraryKeySelection());
	}

	function onSegmentKeydown(event: KeyboardEvent, code: string): void {
		if (event.key !== 'Enter' && event.key !== ' ') return;
		event.preventDefault();
		_toggle(code);
	}
</script>

<div class="library-key-filter-panel" role="dialog" aria-label="Library key filter">
	{#if saveError !== null}
		<p class="save-error" role="alert">{saveError}</p>
	{/if}
	<svg
		class="wheel"
		viewBox="0 0 {LIBRARY_KEY_WHEEL_SIZE} {LIBRARY_KEY_WHEEL_SIZE}"
		role="group"
		aria-label="Camelot key wheel"
	>
		{#each segments as segment (segment.code)}
			{@const selected = shownKeys.includes(segment.code)}
			{@const lines = libraryKeyWheelLabelLines(segment.code, notation)}
			<g
				class="segment"
				class:selected
				class:reference={segment.code === referenceKey}
				data-testid="library-key-segment-{segment.code}"
				data-camelot-key={segment.code}
				role="button"
				tabindex="0"
				aria-pressed={selected}
				aria-label={libraryKeyWheelAriaLabel(segment.code, notation)}
				opacity={libraryKeySegmentOpacity(selected, anySelected)}
				onclick={() => _toggle(segment.code)}
				onkeydown={(e) => onSegmentKeydown(e, segment.code)}
			>
				<path
					d={segment.path}
					style:fill={libraryKeySegmentFill(segment.number, segment.mode)}
					stroke={selected ? '#fff' : 'var(--rb-border-subtle, #333)'}
					stroke-width={selected ? 1.5 : 0.75}
				/>
				<text
					x={segment.labelX}
					y={segment.labelY}
					text-anchor="middle"
					dominant-baseline="middle"
					class="segment-label"
					pointer-events="none"
				>
					{#if lines.line2 !== undefined}
						<tspan x={segment.labelX} dy="-0.35em">{lines.line1}</tspan>
						<tspan x={segment.labelX} dy="1.1em">{lines.line2}</tspan>
					{:else}
						{lines.line1}
					{/if}
				</text>
			</g>
		{/each}
		<text
			x={CENTER}
			y={CENTER}
			text-anchor="middle"
			dominant-baseline="middle"
			class="center-label"
			data-testid="library-key-center"
			pointer-events="none"
		>
			{anySelected ? `${shownKeys.length} key${shownKeys.length === 1 ? '' : 's'}` : 'All keys'}
		</text>
	</svg>
	{#if referenceText !== null}
		<p class="reference-line">Reference: {referenceText}</p>
	{/if}
	<div class="actions">
		<button
			type="button"
			data-testid="library-key-compatible"
			disabled={referenceKey === null}
			title={compatibleTitle}
			onclick={_compatible}>Compatible</button
		>
		<button type="button" data-testid="library-key-reset" disabled={!anySelected} onclick={_reset}
			>Reset</button
		>
		<button type="button" class="close" onclick={onClose}>Close</button>
	</div>
</div>

<style>
	.library-key-filter-panel {
		width: min(280px, calc(100vw - 1rem));
		max-height: min(90vh, 520px);
		overflow: auto;
		padding: 0.5rem;
		border-radius: 8px;
		border: 1px solid var(--rb-border-subtle, #444);
		background: var(--rb-surface-1, #111);
		box-shadow: 0 8px 24px rgb(0 0 0 / 45%);
	}

	.save-error {
		margin: 0 0 0.35rem;
		color: var(--rb-danger, #f66);
		font-size: 0.75rem;
	}

	.reference-line {
		margin: 0 0 0.35rem;
		font-size: 0.7rem;
		opacity: 0.85;
	}

	.wheel {
		display: block;
		width: 100%;
		height: auto;
	}

	.segment {
		cursor: pointer;
	}

	.segment:hover path,
	.segment:focus-visible path {
		stroke: var(--rb-focus-ring, #6af);
		stroke-width: 2;
	}

	.segment:focus-visible {
		outline: 2px solid var(--rb-focus-ring, #6af);
		outline-offset: 0;
	}

	.segment.reference path {
		stroke-dasharray: 3 2;
	}

	.center-label {
		fill: var(--rb-text, #eee);
		font-size: 13px;
		font-weight: 600;
		user-select: none;
	}

	.segment-label {
		fill: #111;
		font-size: 9px;
		font-weight: 600;
		user-select: none;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.35rem;
		margin-top: 0.35rem;
	}

	.actions button {
		font: inherit;
		font-size: 0.72rem;
		padding: 0.2rem 0.45rem;
		border-radius: 4px;
		border: 1px solid var(--rb-border-subtle, #444);
		background: var(--rb-surface-2, #1a1a1a);
		color: inherit;
		cursor: pointer;
	}

	.actions button:disabled {
		opacity: 0.45;
		cursor: default;
	}
</style>
