<script lang="ts">
	/** SETNOTE-6340g: the top-bar sticky-note button; one per `<set-notes-toggle>` slot (see toggle-element.ts).
	 * A dot marks a loaded note that has text in it. */
	import { padUi, setNote, toggleSetNotes } from './store.svelte';

	const hasNote = $derived(setNote.body.trim() !== '');
</script>

<button
	type="button"
	class="sn-toggle"
	class:on={padUi.open}
	class:has-note={hasNote}
	data-testid="set-notes-toggle"
	aria-pressed={padUi.open}
	aria-label={padUi.open ? 'Hide set notes' : 'Show set notes'}
	title={`${padUi.open ? 'Hide' : 'Show'} set notes (or tap right Cmd)${hasNote ? ' - this note has text' : ''}`}
	onclick={toggleSetNotes}
>
	<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
		<path
			d="M2 1.5h10v7.5L9 12.5H2Z"
			fill="var(--sn-fill)"
			stroke="currentColor"
			stroke-width="1.1"
			stroke-linejoin="round"
		/>
		<path
			d="M12 9H9v3.5"
			fill="none"
			stroke="currentColor"
			stroke-width="1.1"
			stroke-linejoin="round"
		/>
		<path d="M4 4.5h5M4 6.8h3.5" stroke="currentColor" stroke-width="1" stroke-linecap="round" />
	</svg>
</button>

<style>
	.sn-toggle {
		--sn-fill: none;
		position: relative;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 22px;
		height: 20px;
		padding: 0;
		border: 0;
		border-radius: 3px;
		background: transparent;
		color: inherit;
		opacity: 0.75;
		cursor: pointer;
	}
	.sn-toggle:hover,
	.sn-toggle:focus-visible {
		opacity: 1;
	}
	.sn-toggle.on {
		--sn-fill: #fbe56b;
		color: #fbe56b;
		opacity: 1;
	}
	.sn-toggle.has-note::after {
		content: '';
		position: absolute;
		top: 1px;
		right: 2px;
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: #fbe56b;
	}
</style>
