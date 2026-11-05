<script lang="ts">
	/**
	 * GRID-OCTAVE-02: the small mark beside a BPM whose tempo our analysis and
	 * Mixed In Key disagree on. An inline SVG glyph (CHROME-01: no emoji, no
	 * text symbol); the explanation lives in the host's hover card.
	 */
	import { BPM_DISPUTE_PATH } from '$lib/ui/icon-glyphs';
</script>

<svg class="bpm-dispute-glyph" viewBox="0 0 12 12" width="10" height="10" aria-hidden="true" focusable="false">
	<path d={BPM_DISPUTE_PATH} fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" stroke-linecap="round" />
</svg>

<style>
	.bpm-dispute-glyph {
		display: inline-block;
		vertical-align: -1px;
		margin-left: 3px;
		color: var(--rb-orange, #e8a13a);
	}
</style>
