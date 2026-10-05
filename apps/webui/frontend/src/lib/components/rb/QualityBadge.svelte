<!--
	Venue-rung quality badge: "the biggest room this file survives".

	Source of truth is apps/shared/audio_quality.py, served inline on every
	track row (contract 1/4) and on rb-meta. This component renders what the
	backend measured and NOTHING else: when venue is null the badge says
	'Unknown' and the title carries the backend's reason. No guessed rung,
	ever (house rule: no mocked data).

	Colour ramps across the six rungs using the existing rekordbox palette
	vars only (theme.css): dim grey at the naughty step, through the amber
	mid-rungs, to the lit blue/white of stadium.
-->
<script lang="ts">
	import type { TrackQuality } from '$lib/rb/library-types';

	interface Props {
		quality: TrackQuality | null;
		/** Hide the container suffix where the row is very tight. */
		showContainer?: boolean;
		/** Initial only (C, W, S...) instead of the rung name. The colour and
		 * the hover title carry the rest, and the column stops paying for a
		 * word it repeats on every row (pin f875e746d40a). */
		compact?: boolean;
	}

	const { quality, showContainer = true, compact = false }: Props = $props();

	/** '.mp3' -> 'mp3'; '' stays '' so nothing renders a bare dot. */
	function _ext(container: string): string {
		return container.startsWith('.') ? container.slice(1) : container;
	}

	/** The mandatory hover title: blurb + the measured numbers behind it. */
	function _title(q: TrackQuality | null): string {
		if (q === null) return 'audio quality not loaded yet for this row';
		const ext = _ext(q.container);
		if (q.venue === null) {
			// q.blurb is the backend's REASON here, not a rung description.
			return `Quality unknown -- ${q.blurb}${ext ? `, ${ext}` : ''}.`;
		}
		const measured =
			q.kbps === null
				? 'bitrate not measurable'
				: `Effective ${q.kbps} kbps`;
		const lossless = q.lossless ? ' Lossless container, so the rung is set by format, not bitrate.' : '';
		return (
			`${q.label} -- ${q.blurb}. ${measured}${ext ? `, ${ext}` : ''}.` +
			` Rung ${(q.rank ?? 0) + 1} of ${q.of}.${lossless}`
		);
	}

	const full = $derived(quality === null ? '...' : quality.label);
	const label = $derived(compact ? full.slice(0, 1).toUpperCase() : full);
	const ext = $derived(quality === null ? '' : _ext(quality.container));
	const rung = $derived(quality === null || quality.venue === null ? 'unknown' : quality.venue);
</script>

<span class="q-badge q-{rung}" class:compact title={_title(quality)} data-venue={rung}>
	<span class="q-label">{label}</span>
	{#if showContainer && ext}<span class="q-ext">{ext}</span>{/if}
</span>

<style>
	.q-badge {
		display: inline-flex;
		align-items: baseline;
		gap: 4px;
		max-width: 100%;
		padding: 0 5px;
		border: 1px solid var(--rb-border, #23282f);
		border-radius: 3px;
		background: var(--rb-panel-raised, #1a1e25);
		color: var(--rb-text-dim, #838990);
		font-size: 9px;
		line-height: 15px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		/* Skin hook: Gothic (mono-dev) grays the whole ramp (theme.css). */
		filter: var(--rb-quality-filter, none);
	}

	.q-label {
		font-weight: 600;
		letter-spacing: 0.02em;
	}
	/* One glyph, centred, so the column can be as narrow as the letter. */
	.q-badge.compact {
		padding: 0 3px;
		justify-content: center;
		min-width: 12px;
	}

	.q-ext {
		opacity: 0.65;
		font-size: 8px;
		text-transform: lowercase;
	}

	/* ---- the ramp: dim at the bottom rung, lit at the top ---- */
	.q-unknown {
		color: var(--rb-text-dim, #838990);
		border-style: dashed;
		background: transparent;
	}

	.q-naughty_step {
		color: var(--rb-red, #d0342c);
		border-color: rgba(208, 52, 44, 0.45);
		background: rgba(208, 52, 44, 0.1);
	}

	.q-lounge {
		color: var(--rb-text-dim, #838990);
		border-color: var(--rb-border, #23282f);
	}

	.q-house_party {
		color: var(--rb-yellow, #e5c33a);
		border-color: rgba(229, 195, 58, 0.35);
		background: rgba(229, 195, 58, 0.08);
	}

	.q-club {
		color: var(--rb-orange, #e8a13a);
		border-color: rgba(232, 161, 58, 0.4);
		background: rgba(232, 161, 58, 0.1);
	}

	.q-warehouse {
		color: var(--rb-green, #35c04f);
		border-color: rgba(53, 192, 79, 0.45);
		background: rgba(53, 192, 79, 0.12);
	}

	.q-stadium {
		color: var(--rb-wave-high, #cfe0f2);
		border-color: var(--rb-accent, #2f6fd6);
		background: rgba(47, 111, 214, 0.22);
		box-shadow: 0 0 4px var(--rb-accent-glow, rgba(47, 111, 214, 0.55));
	}
</style>
