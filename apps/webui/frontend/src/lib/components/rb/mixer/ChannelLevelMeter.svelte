<script lang="ts">
	/**
	 * Channel level meter: ten discrete segments, read POST-EQ and POST-FADER.
	 *
	 * The reading arrives from an AudioWorklet tap on the channel strip, and
	 * every number below (dB scale, segment thresholds, color bands, PPM
	 * ballistics) comes from `$lib/rb/meter-math`, which is unit tested without
	 * an AudioContext. This component decides nothing.
	 *
	 * TWO THINGS CHANGED HERE, and both were wrong in ways that looked fine:
	 *
	 * 1. Segments were lit as `ceil(meter * 10)` against a LINEAR amplitude.
	 *    Music sits far below full scale in linear terms, so the top segments
	 *    were effectively unreachable and the bar behaved like an on/off lamp.
	 *    Segment thresholds are spaced in dB now.
	 * 2. The meter must tap after the actual channel-volume fader. A tap before
	 *    it can make the slider change audio while this indicator stays still.
	 *
	 * Red means digital clipping at THIS point in the chain. It is not a
	 * speaker-damage reading and must never be labelled as one: everything
	 * downstream (master gain, OS volume, the interface, the amplifier) is
	 * invisible from here. See (private path).
	 */
	import { onDestroy, untrack } from 'svelte';

	import { peekDeckMeterReading } from '$lib/rb/audio-engine.svelte';
	import {
		METER_FLOOR_DBFS,
		clipHeldUntil,
		clipLit,
		createClipPipCursor,
		segmentBand,
		segmentsLitFromDbfs,
		segmentThresholdsForRed
	} from '$lib/rb/meter-math';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	interface Props {
		// Derived rather than imported from deck-slots on purpose: this keeps one
		// import edge off a module that is already near the fan-in allowance.
		deckId: Parameters<typeof peekDeckMeterReading>[0];
		playing?: boolean;
	}

	let { deckId, playing = false }: Props = $props();

	// #1475: the whole scale shifts so the by-ear captured level becomes the
	// first red segment. `red_enabled` false (the default, uncalibrated case)
	// passes null through, which is the documented no-op input that keeps this
	// identical to the fixed scale. Computed here, not in meter-tap, so the tap
	// stays deck-agnostic and this component owns the only policy choice.
	const thresholds = $derived(
		segmentThresholdsForRed(
			uiPrefs.level_calibration.red_enabled ? uiPrefs.level_calibration.red_dbfs : null
		)
	);
	/** One entry per segment, colored by the shared band policy. */
	const SEGMENTS = $derived(
		thresholds.map((threshold, index) => ({
			threshold,
			band: segmentBand(index + 1)
		}))
	);

	let lit = $state(0);
	let db = $state(METER_FLOOR_DBFS);
	let clipped = $state(false);
	let raf = 0;
	let clipUntil = 0;
	/** This meter's own cursor on the tap's post-crossfader clips, seeded when
	 * playback activates (see createClipPipCursor). */
	const pipCursor = createClipPipCursor();

	$effect(() => {
		const live = playing;
		cancelAnimationFrame(raf);
		if (!live) {
			lit = 0;
			db = METER_FLOOR_DBFS;
			clipped = false;
			clipUntil = 0;
			pipCursor.reset();
			return;
		}
		pipCursor.activate(untrack(() => peekDeckMeterReading(deckId).heardClipSeq));
		const tick = (now: number): void => {
			const reading = peekDeckMeterReading(deckId);
			// Orange/red segments follow the post-channel-fader tap (`db`), the
			// same point the volume slider drives (pin `3dc62dbb909c`, issue
			// #11321). The clip pip alone uses post-crossfader `heard` peaks
			// (Sol P1, #6829) so a crossfader move never rewrites a latched clip.
			const channelDb = reading.db;
			lit = segmentsLitFromDbfs(channelDb, thresholds);
			db = channelDb;
			// The pip holds like the tap's own latch, or a one-frame clip never
			// shows. It lights on any post-crossfader clip the tap recorded since
			// the last frame, judged on the audio thread through the crossfader
			// those samples played under, never the decaying level (Sol/Codex, #6829).
			const freshClip = pipCursor.frame(reading.heardClipSeq);
			clipUntil = clipHeldUntil(freshClip, now, clipUntil);
			clipped = clipLit(now, clipUntil);
			raf = requestAnimationFrame(tick);
		};
		raf = requestAnimationFrame(tick);
		return () => cancelAnimationFrame(raf);
	});

	onDestroy(() => cancelAnimationFrame(raf));

	// Every numeric readout carries what the number actually is, per house rule.
	const readout = $derived(
		db <= METER_FLOOR_DBFS ? 'silent' : `${db.toFixed(1)} dBFS${clipped ? ' - CLIPPING' : ''}`
	);
</script>

<div
	class="rb-channel-level-meter"
	class:clipped
	role="meter"
	aria-label={`channel ${deckId} level, post-EQ post-channel-fader`}
	aria-valuemin={METER_FLOOR_DBFS}
	aria-valuemax={0}
	aria-valuenow={db}
	aria-valuetext={readout}
	title={`Post-EQ, post-channel-fader level: ${readout}. Responds to trim, EQ, and the channel volume fader (pin 3dc62dbb909c). The clip pip alone follows the post-crossfader path. Red means digital clipping at this tap, not speaker risk.`}
>
	<!--
		Keyed by index, not segment.threshold: a low red-calibration anchor clamps
		several thresholds to the same METER_FLOOR_DBFS value (see
		segmentThresholdsForRed), so threshold is not a unique key. SEGMENTS is a
		fixed-length array in a fixed order (one entry per index, always the same
		count), so index is always unique and stable across recalibration.
	-->
	{#each SEGMENTS as segment, index (index)}
		<div
			class:lit={index < lit}
			class:clip-latch={clipped && index === SEGMENTS.length - 1}
			class:green={segment.band === 'green'}
			class:amber={segment.band === 'amber'}
			class:red={segment.band === 'red'}
			class="rb-channel-level-meter-segment"
			aria-hidden="true"
		></div>
	{/each}
</div>

<style>
	.rb-channel-level-meter {
		position: absolute;
		left: 50%;
		bottom: 0;
		z-index: 1;
		display: flex;
		flex-direction: column-reverse;
		gap: 2px;
		width: 4px;
		height: 100%;
		transform: translateX(-50%);
		pointer-events: none;
	}
	.rb-channel-level-meter-segment {
		min-height: 2px;
		flex: 1;
		border-radius: 1px;
		opacity: 0.18;
	}
	.rb-channel-level-meter-segment.green {
		background: #35c04f;
	}
	.rb-channel-level-meter-segment.amber {
		background: #e8a13a;
	}
	.rb-channel-level-meter-segment.red {
		background: #e23a32;
	}
	.rb-channel-level-meter-segment.lit {
		opacity: 0.85;
		box-shadow: 0 0 3px currentColor;
	}
	.rb-channel-level-meter-segment.green.lit {
		color: #35c04f;
	}
	.rb-channel-level-meter-segment.amber.lit {
		color: #e8a13a;
	}
	.rb-channel-level-meter-segment.red.lit {
		color: #e23a32;
	}
	/* The clip latch outlives the sample that caused it, so a single overshoot
	   is readable rather than a one-frame flash nobody sees. Scoped to `.lit`:
	   without it, a latched clip painted every red segment regardless of the
	   current level, so continuous clipping read as permanently red even as
	   the level moved (#1475). */
	/* The clip latch gets its OWN segment rather than repainting the red band.
	   Scoping the old rule to `.lit` (which stopped a latch painting segments
	   that were not lit) also made the latch invisible the moment the level
	   fell back below red -- exactly the brief overshoot the latch exists to
	   show. The top segment is the designated clip marker: it lights on a
	   latch regardless of level, so a transient is readable without the bar
	   claiming a level it does not have. */
	.rb-channel-level-meter-segment.clip-latch {
		opacity: 1;
		background: #e23a32;
		color: #e23a32;
		box-shadow: 0 0 5px #e23a32;
	}
	.rb-channel-level-meter.clipped .rb-channel-level-meter-segment.red.lit {
		opacity: 1;
		color: #e23a32;
		box-shadow: 0 0 5px #e23a32;
	}
</style>
