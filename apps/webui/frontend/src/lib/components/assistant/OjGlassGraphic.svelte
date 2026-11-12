<script lang="ts">
	import type { OjMotion } from './oj-motion-policy';
	import type { OjGlassState } from './oj-glass-gestures';

	let {
		classes,
		state,
		bubbles,
		effective,
		size,
		clipId
	}: {
		classes: string;
		state: OjGlassState;
		bubbles: boolean;
		effective: OjMotion;
		size: number;
		clipId: string;
	} = $props();
</script>

<svg
	class={classes}
	role="img"
	aria-label="OJ"
	width={size}
	height={size}
	viewBox="0 0 28 28"
	data-oj-state={state}
>
	<title>OJ</title>
	<g class="body">
		<defs>
			<clipPath id={clipId}>
				<path d="M8 6 L8 22 Q8 24 10 24 L18 24 Q20 24 20 22 L20 6 Q20 4 18 4 L10 4 Q8 4 8 6 Z" />
			</clipPath>
		</defs>
		<g clip-path="url(#{clipId})">
			<g class="juice">
				<path class="juice-fill" d="M7 15 L21 15 L21 24 L7 24 Z" fill="var(--accent)" />
			</g>
			{#if bubbles && effective.bubbles}
				<g class="bubbles" aria-hidden="true">
					<circle
						data-oj-glass-bubble
						class="bubble b1"
						cx="11"
						cy="18"
						r="1.2"
						fill="var(--accent)"
					/>
					<circle
						data-oj-glass-bubble
						class="bubble b2"
						cx="14"
						cy="19"
						r="1"
						fill="var(--accent)"
					/>
					<circle
						data-oj-glass-bubble
						class="bubble b3"
						cx="17"
						cy="17.5"
						r="0.9"
						fill="var(--accent)"
					/>
				</g>
			{/if}
		</g>
		<path
			class="outline"
			d="M8 6 L8 22 Q8 24 10 24 L18 24 Q20 24 20 22 L20 6 Q20 4 18 4 L10 4 Q8 4 8 6 Z"
			fill="none"
			stroke="currentColor"
			stroke-width="1.6"
		/>
		<g class="face">
			<g class="eyes">
				<circle class="eye" cx="11.5" cy="11" r="1.4" fill="currentColor" />
				<circle class="eye" cx="16.5" cy="11" r="1.4" fill="currentColor" />
			</g>
			<path
				class="mouth"
				d="M11.5 13.5 Q14 15.5 16.5 13.5"
				fill="none"
				stroke="currentColor"
				stroke-width="1.2"
				stroke-linecap="round"
			/>
			<g class="think-dots" aria-hidden="true">
				<circle class="dot d1" cx="19" cy="8" r="0.8" fill="currentColor" />
				<circle class="dot d2" cx="21" cy="6.5" r="0.7" fill="currentColor" />
				<circle class="dot d3" cx="23" cy="8.5" r="0.6" fill="currentColor" />
			</g>
		</g>
	</g>
</svg>

<style>
	.oj-glass {
		color: var(--fg);
		display: inline-block;
		flex: none;
		overflow: visible;
	}

	.oj-glass .mouth {
		opacity: 0;
	}

	.oj-glass .think-dots {
		opacity: 0;
	}

	.oj-glass.smile .mouth,
	.oj-glass.gesture-smile .mouth {
		opacity: 1;
	}

	.oj-glass.think .think-dots {
		opacity: 1;
	}

	.oj-glass .body {
		transform-box: fill-box;
		transform-origin: 50% 85%;
	}

	.oj-glass .juice {
		transform-box: fill-box;
		transform-origin: 50% 100%;
	}

	.oj-glass .eyes {
		transform-box: fill-box;
		transform-origin: 50% 50%;
	}

	.oj-glass.idle .eyes {
		animation: oj-blink 4s infinite;
	}

	.oj-glass.gesture-smile .mouth {
		animation: oj-smile 1.2s ease forwards;
	}

	.oj-glass.slosh .juice {
		animation: oj-slosh 1.2s ease-in-out infinite;
	}

	.oj-glass.slosh .body {
		animation: oj-tilt 1.2s ease-in-out infinite;
	}

	.oj-glass.gesture-slosh .juice {
		animation: oj-slosh 1.2s ease forwards;
	}

	.oj-glass.gesture-slosh .body {
		animation: oj-tilt 1.2s ease forwards;
	}

	.oj-glass.think .dot.d1 {
		animation: oj-think 1.2s ease-in-out infinite;
	}

	.oj-glass.think .dot.d2 {
		animation: oj-think 1.2s ease-in-out 0.2s infinite;
	}

	.oj-glass.think .dot.d3 {
		animation: oj-think 1.2s ease-in-out 0.4s infinite;
	}

	.oj-glass .bubble {
		transform-box: fill-box;
		transform-origin: 50% 100%;
		opacity: 0;
	}

	.oj-glass .b1 {
		animation: oj-bubble 2.4s ease-in-out infinite;
	}

	.oj-glass .b2 {
		animation: oj-bubble 2.4s ease-in-out 0.5s infinite;
	}

	.oj-glass .b3 {
		animation: oj-bubble 2.4s ease-in-out 1s infinite;
	}

	.oj-glass.still,
	.oj-glass.still :global(*) {
		animation: none !important;
	}

	@keyframes oj-blink {
		0%,
		96%,
		100% {
			transform: scaleY(1);
		}
		98% {
			transform: scaleY(0.1);
		}
	}

	@keyframes oj-smile {
		0% {
			opacity: 0;
		}
		20%,
		80% {
			opacity: 1;
		}
		100% {
			opacity: 0;
		}
	}

	@keyframes oj-slosh {
		0% {
			transform: rotate(0deg);
		}
		25% {
			transform: rotate(-6deg);
		}
		50% {
			transform: rotate(5deg);
		}
		75% {
			transform: rotate(-2deg);
		}
		100% {
			transform: rotate(0deg);
		}
	}

	@keyframes oj-tilt {
		0% {
			transform: rotate(0deg);
		}
		50% {
			transform: rotate(3deg);
		}
		100% {
			transform: rotate(0deg);
		}
	}

	@keyframes oj-think {
		0%,
		100% {
			opacity: 0.2;
		}
		50% {
			opacity: 1;
		}
	}

	@keyframes oj-bubble {
		0% {
			transform: translateY(0);
			opacity: 0;
		}
		30% {
			opacity: 0.9;
		}
		100% {
			transform: translateY(-8px);
			opacity: 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.oj-glass,
		.oj-glass :global(*) {
			animation: none !important;
		}
	}
</style>
