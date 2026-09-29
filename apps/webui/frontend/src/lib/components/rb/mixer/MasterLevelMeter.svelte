<script lang="ts">
	/**
	 * Master output level meter: ten discrete segments, read POST MASTER GAIN.
	 *
	 * Pin 5a5c3b8033d8's still-open half. PR #1062 already shipped the
	 * ten-segment green/amber/red no-gradient CHANNEL meters
	 * (`ChannelLevelMeter.svelte`), which tap post-trim, post-EQ, and
	 * post-channel-fader (issue #3529) -- so they track each deck's channel
	 * fader, not the master volume control. the maintainer's "red = speakers might be
	 * damaged - so overall volume effects them" is about the MASTER bus,
	 * which is what this component reads.
	 *
	 * NOT factored into a shared component with ChannelLevelMeter, on purpose:
	 * that component's markup and thresholds are pinned VERBATIM by
	 * `tests/unit/pitch-fader-geometry.test.mjs` (source-level regression for
	 * pin 5a5c3b8033d8's already-shipped half), and this meter's layout is
	 * horizontal (next to the horizontal master slider in TopBar) where the
	 * channel meter's is vertical (over a vertical fader) -- reworking both
	 * into one orientation-aware component would touch code this pin
	 * explicitly says to leave alone for a cosmetic gain. What IS shared,
	 * and is the entire point per the research doc, is the POLICY: segment
	 * thresholds, colour bands and PPM ballistics all come from
	 * `meter-math.ts`, and the measurement itself reuses `meter-tap.ts`
	 * (written deck-agnostic for exactly this reuse) via
	 * `peekMasterMeterReading` in audio-engine.svelte.ts. Nothing here
	 * invents a second set of numbers.
	 *
	 * HONESTY IN THE LABEL IS LOAD-BEARING, and is itself part of the pin.
	 * This tap sits post master-gain, so it is closer to "what reaches the
	 * speakers" than the channel meter (it includes the master volume
	 * control), but it is still not a speaker-damage reading: this app
	 * cannot see the speakers. OS volume, the audio interface, the
	 * amplifier and its limiter are all downstream of this tap and
	 * invisible from here. Red means digital clipping / out of headroom at
	 * the master bus, not a prediction of speaker or amplifier damage,
	 * which would need the amp's gain and the speakers' power handling.
	 *
	 * ONE MORE EXCEPTION, and it is real: when external routing sends a deck
	 * straight to its own USB output (`parseExternalRouting()` in
	 * audio-engine.svelte.ts), that deck bypasses `_masterGain` and the
	 * crossfader entirely, so this meter can read near-silence while that
	 * deck plays audibly loud. This meter is a reading of the master bus,
	 * not of every deck's actual output.
	 * See docs/research/adrian-level-meters-clipping-lights.md.
	 */
	import { onDestroy } from 'svelte';

	import {
		metersUnavailable,
		onMetersUnavailableChange,
		peekMasterMeterReading
	} from '$lib/rb/audio-engine.svelte';
	import {
		METER_FLOOR_DBFS,
		SEGMENT_THRESHOLDS_DBFS,
		segmentBand
	} from '$lib/rb/meter-math';

	interface Props {
		/** Gate for the RAF loop below. TopBar mounts this for the whole
		 * /performance session, so the default is `false` (fail-safe, no
		 * hidden always-on loop): the caller must opt the meter in by passing
		 * `active`, the same way VFader/ChannelLevelMeter require a `playing`
		 * prop rather than defaulting to on. */
		active?: boolean;
	}

	let { active = false }: Props = $props();

	/** One entry per segment, colored by the shared band policy. */
	const SEGMENTS = SEGMENT_THRESHOLDS_DBFS.map((threshold, index) => ({
		threshold,
		band: segmentBand(index + 1)
	}));

	let lit = $state(0);
	let db = $state(METER_FLOOR_DBFS);
	let clipped = $state(false);
	// True once the worklet failed to arm (AGENTS.md L244-L246: a terminal
	// processor error must be surfaced, not masked as an ordinary silent
	// reading). Distinct from "nothing playing" (lit=0, active=false renders
	// the same bars) - this renders a visibly different, honestly-labelled
	// state so a broken meter is never mistaken for a quiet master bus.
	let unavailable = $state(metersUnavailable());
	let raf = 0;

	// Independent of the RAF loop below, and that is the point: the graph is
	// often armed while nothing is playing, so a terminal worklet failure has
	// to reach this component without a poll the idle gate has switched off.
	// Reading the current verdict once and then listening for changes covers
	// both the arm-before-play and the fail-while-playing orders.
	$effect(() => onMetersUnavailableChange((next) => { unavailable = next; }));

	$effect(() => {
		const live = active;
		cancelAnimationFrame(raf);
		if (!live) {
			// The LEVEL resets, the VERDICT does not: an idle meter has no level
			// to show, but a worklet that failed to arm is still broken and must
			// keep saying so. Clearing `unavailable` here is what made a
			// terminal failure indistinguishable from silence whenever playback
			// stopped, or whenever the graph was built while nothing played.
			lit = 0;
			db = METER_FLOOR_DBFS;
			clipped = false;
			return;
		}
		const tick = (): void => {
			const reading = peekMasterMeterReading();
			lit = reading.segments;
			db = reading.db;
			clipped = reading.clipped;
			raf = requestAnimationFrame(tick);
		};
		raf = requestAnimationFrame(tick);
		return () => cancelAnimationFrame(raf);
	});

	onDestroy(() => cancelAnimationFrame(raf));

	// Every numeric readout carries what the number actually is, per house rule.
	const readout = $derived(
		unavailable
			? 'unavailable'
			: db <= METER_FLOOR_DBFS
				? 'silent'
				: `${db.toFixed(1)} dBFS${clipped ? ' - CLIPPING' : ''}`
	);

	const label = $derived(
		unavailable
			? 'level unavailable - the meter failed to start'
			: `Master output level, post master gain: ${readout}. Moves with the master volume control. Closer to what reaches the speakers than a channel meter, but still not a speaker-damage reading -- OS volume, the interface, the amp and its limiter are all downstream and invisible from here. Red means digital clipping / out of headroom, not confirmed speaker risk. Reads the master bus only: a deck routed to its own USB output skips this bus entirely and will not move this meter.`
	);
</script>

<div
	class="rb-master-level-meter"
	class:clipped
	class:unavailable
	role="meter"
	aria-label={unavailable
		? 'master output level unavailable - the meter failed to start'
		: 'master output level, post master gain'}
	aria-valuemin={METER_FLOOR_DBFS}
	aria-valuemax={0}
	aria-valuenow={db}
	aria-valuetext={readout}
	title={label}
>
	{#each SEGMENTS as segment, index (segment.threshold)}
		<div
			class:lit={!unavailable && index < lit}
			class:green={segment.band === 'green'}
			class:amber={segment.band === 'amber'}
			class:red={segment.band === 'red'}
			class="rb-master-level-meter-segment"
			aria-hidden="true"
		></div>
	{/each}
</div>

<style>
	.rb-master-level-meter {
		display: flex;
		flex-direction: row;
		gap: 2px;
		width: 80px;
		height: 4px;
		pointer-events: auto;
	}
	.rb-master-level-meter-segment {
		min-width: 2px;
		flex: 1;
		border-radius: 1px;
		opacity: 0.18;
	}
	.rb-master-level-meter-segment.green {
		background: #35c04f;
	}
	.rb-master-level-meter-segment.amber {
		background: #e8a13a;
	}
	.rb-master-level-meter-segment.red {
		background: #e23a32;
	}
	.rb-master-level-meter-segment.lit {
		opacity: 0.85;
		box-shadow: 0 0 3px currentColor;
	}
	.rb-master-level-meter-segment.green.lit {
		color: #35c04f;
	}
	.rb-master-level-meter-segment.amber.lit {
		color: #e8a13a;
	}
	.rb-master-level-meter-segment.red.lit {
		color: #e23a32;
	}
	/* The clip latch outlives the sample that caused it, so a single overshoot
	   is readable rather than a one-frame flash nobody sees. */
	.rb-master-level-meter.clipped .rb-master-level-meter-segment.red {
		opacity: 1;
		color: #e23a32;
		box-shadow: 0 0 5px #e23a32;
	}
	/* Visibly distinct from every other state: a broken meter must never
	   look like a quiet bar (all segments unlit, same shape as silent). */
	.rb-master-level-meter.unavailable .rb-master-level-meter-segment {
		background: repeating-linear-gradient(
			45deg,
			#7a7a7a,
			#7a7a7a 2px,
			#3a3a3a 2px,
			#3a3a3a 4px
		);
		opacity: 0.7;
	}
</style>
