<script lang="ts">
	/**
	 * The status mark beside a MIDI entry's label (CHROME-07): a tick when a
	 * mapped controller is bound, a cross when MIDI is on with none bound, and
	 * nothing otherwise. Both mixer MIDI entries (the headphone-cluster button
	 * and the one inside the audio I/O menu) render THIS, from the one
	 * midiLabelGlyph() derivation, so the two cannot drift apart.
	 */
	import { MIDI_CROSS_PATH, MIDI_TICK_PATH, type MidiLabelGlyphKind } from '$lib/ui/icon-glyphs';

	let { glyph }: { glyph: MidiLabelGlyphKind } = $props();
</script>

{#if glyph !== 'none'}<svg
		class="midi-glyph"
		data-midi-glyph={glyph}
		viewBox="0 0 12 12"
		width="9"
		height="9"
		aria-hidden="true"
		><path
			d={glyph === 'tick' ? MIDI_TICK_PATH : MIDI_CROSS_PATH}
			fill="none"
			stroke="currentColor"
			stroke-width="1.6"
		/></svg
	>{/if}

<style>
	.midi-glyph {
		margin-left: 3px;
		vertical-align: middle;
	}
</style>
