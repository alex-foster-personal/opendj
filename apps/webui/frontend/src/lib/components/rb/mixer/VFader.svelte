<script lang="ts">
	/**
	 * Vertical channel fader (blue line thumb, SCREENSHOT-SPEC 4).
	 * Uses the shared .rb-fader chrome from theme.css. Real control:
	 * click/drag anywhere on the track sets the value.
	 */
	interface Props {
		/** 0..1; 1 = full (thumb at top). */
		value: number;
		/** Called with the new 0..1 value while dragging. */
		onchange: (value: number) => void;
		/** Accessible name, e.g. 'channel 3 fader'. */
		label: string;
	}

	let { value, onchange, label }: Props = $props();

	const TRACK_H = 96; // .rb-fader height in theme.css
	const THUMB_H = 12; // .rb-fader-thumb height in theme.css

	let dragging = false;

	const thumbTopPx = $derived((1 - value) * (TRACK_H - THUMB_H));

	function _clamp01(v: number): number {
		return Math.min(1, Math.max(0, v));
	}

	function _valueFromEvent(e: PointerEvent): number {
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		const y = e.clientY - rect.top - THUMB_H / 2;
		return _clamp01(1 - y / (TRACK_H - THUMB_H));
	}

	function handlePointerDown(e: PointerEvent): void {
		dragging = true;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
		onchange(_valueFromEvent(e));
	}

	function handlePointerMove(e: PointerEvent): void {
		if (!dragging) return;
		onchange(_valueFromEvent(e));
	}

	function handlePointerUp(e: PointerEvent): void {
		if (!dragging) return;
		dragging = false;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
	}

	function handleKeyDown(e: KeyboardEvent): void {
		if (e.key === 'ArrowUp') {
			e.preventDefault();
			onchange(_clamp01(value + 0.02));
		} else if (e.key === 'ArrowDown') {
			e.preventDefault();
			onchange(_clamp01(value - 0.02));
		}
	}
</script>

<div
	class="rb-fader"
	role="slider"
	aria-label={label}
	aria-orientation="vertical"
	aria-valuemin={0}
	aria-valuemax={1}
	aria-valuenow={value}
	tabindex="0"
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={handlePointerUp}
	onkeydown={handleKeyDown}
>
	<div class="rb-fader-track"></div>
	<div class="rb-fader-thumb" style={`top: ${thumbTopPx}px;`}></div>
</div>

<style>
	.rb-fader {
		cursor: ns-resize;
		touch-action: none;
		outline: none;
	}
	.rb-fader:focus-visible .rb-fader-thumb {
		box-shadow: 0 0 4px var(--rb-accent-glow);
	}
</style>
