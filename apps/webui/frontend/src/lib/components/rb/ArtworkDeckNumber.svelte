<script lang="ts">
	/**
	 * ARTNUM-9649b: the deck number as a white outline with a dark halo, centered
	 * over a deck's artwork square. Props in, nothing global: the parent
	 * passes the pref and the square's size. The parent must be position:
	 * relative, with the cover <img> (or <video>) beside this overlay.
	 *
	 * ARTNUM-11719: glow adds an outer glow in the art's dominant colour; tinted
	 * also shades the halo from it. The stroke stays white in every mode. The
	 * colour is sampled once per stable_id from the sibling cover (8x8 canvas,
	 * no request); a tainted canvas draws as halo. Animated covers resample at
	 * 1 Hz while the deck plays. Grayscale skins (Gothic, Light) filter the art,
	 * so the tint swaps to its grayscale there and tinted renders neutral; the
	 * colour returns when the art's own hover lifts the filter.
	 */
	import {
		createTintResampler,
		deckNumberArtGlowVars,
		deckNumberArtVisible,
		dominantColorOf,
		effectiveDeckNumberArt,
		isAnimatedCover,
		DECK_NUMBER_ART_RESAMPLE_MS,
		type DeckNumberArt,
		type Rgb
	} from '$lib/rb/deck-number-art';

	let {
		deck,
		mode,
		sizePx,
		stableId,
		playing,
		tint
	}: {
		deck: number;
		mode: DeckNumberArt;
		sizePx: number;
		/** The loaded track's stable_id, the sampler's cache key; null for none. */
		stableId: string | null;
		/** The deck plays: drives the 1 Hz resampler for animated covers. */
		playing: boolean;
		/** A fixed colour (stories, SSR tests); skips sampling. */
		tint?: Rgb;
	} = $props();

	let el = $state<HTMLSpanElement | null>(null);
	let sampled = $state<Rgb | null>(null);
	let resampler = $state<ReturnType<typeof createTintResampler> | null>(null);

	const needsTint = $derived(mode === 'glow' || mode === 'tinted');
	const color = $derived(tint ?? sampled);
	const effective = $derived(effectiveDeckNumberArt(mode, color));
	const glowVars = $derived(
		(effective === 'glow' || effective === 'tinted') && color !== null
			? deckNumberArtGlowVars(effective, color)
			: null
	);
	const styleText = $derived(
		`--art-s: ${sizePx}px;` +
			(glowVars === null
				? ''
				: Object.entries(glowVars)
						.map(([k, v]) => ` ${k}: ${v};`)
						.join(''))
	);

	function _mediaReady(media: HTMLImageElement | HTMLVideoElement): boolean {
		return media instanceof HTMLVideoElement
			? media.readyState >= 2
			: media.complete && media.naturalWidth > 0;
	}

	$effect(() => {
		if (!needsTint || tint !== undefined || stableId === null || el === null) return;
		const id = stableId;
		const media = el.parentElement?.querySelector<HTMLImageElement | HTMLVideoElement>(
			'img, video'
		);
		if (!media)
			throw new Error('ArtworkDeckNumber: no <img> or <video> beside the overlay to sample');
		sampled = null;
		const sample = (refresh: boolean): void => {
			sampled = dominantColorOf(media, id, { refresh });
		};
		const event = media instanceof HTMLVideoElement ? 'loadeddata' : 'load';
		const onReady = (): void => sample(false);
		if (_mediaReady(media)) sample(false);
		else media.addEventListener(event, onReady, { once: true });
		const r = isAnimatedCover(media)
			? createTintResampler(() => sample(true), DECK_NUMBER_ART_RESAMPLE_MS)
			: null;
		resampler = r;
		return () => {
			media.removeEventListener(event, onReady);
			r?.stop();
			resampler = null;
		};
	});

	$effect(() => {
		resampler?.setPlaying(playing);
	});
</script>

{#if deckNumberArtVisible(mode, sizePx)}
	<span
		bind:this={el}
		class="deck-num-art"
		class:glow={effective === 'glow'}
		class:tinted={effective === 'tinted'}
		style={styleText}
		data-mode={effective}
		aria-hidden="true"
		data-testid="artwork-deck-number">{deck}</span
	>
{/if}

<style>
	.deck-num-art {
		position: absolute;
		inset: 0;
		display: grid;
		place-items: center;
		pointer-events: none;
		font-family: var(--rb-font-brand);
		font-weight: 800;
		font-stretch: 150%;
		font-size: calc(var(--art-s) * 0.5);
		line-height: 1;
		color: transparent;
		-webkit-text-stroke: max(1px, calc(var(--art-s) * 0.035)) #fff;
		/* Halo: mimicked from Material's text-on-image scrim guidance. */
		filter: drop-shadow(0 0 calc(var(--art-s) * 0.06) rgb(0 0 0 / 0.85))
			drop-shadow(0 0 calc(var(--art-s) * 0.12) rgb(0 0 0 / 0.6));
		--dna-halo-now: var(--dna-halo);
		--dna-glow-now: var(--dna-glow);
	}
	/* Glow and tinted: the halo plus an outer glow (radius 0.2, alpha 0.55 of
	   the square, DECK_NUMBER_ART_GLOW_*), mimicked from the same scrim guidance. */
	.deck-num-art.glow,
	.deck-num-art.tinted {
		filter: drop-shadow(
				0 0 calc(var(--art-s) * 0.06) color-mix(in srgb, var(--dna-halo-now) 85%, transparent)
			)
			drop-shadow(
				0 0 calc(var(--art-s) * 0.12) color-mix(in srgb, var(--dna-halo-now) 60%, transparent)
			)
			drop-shadow(
				0 0 calc(var(--art-s) * 0.2) color-mix(in srgb, var(--dna-glow-now) 55%, transparent)
			);
		transition: filter 120ms ease-out;
	}
	/* Grayscale skins filter the art, so the tint follows the filtered colour. */
	:global(html:is([data-skin='mono-dev'], [data-skin='light'])) .deck-num-art {
		--dna-halo-now: var(--dna-halo-gray);
		--dna-glow-now: var(--dna-glow-gray);
	}
	:global(html:is([data-skin='mono-dev'], [data-skin='light']) img[src*='/artwork']:hover)
		+ .deck-num-art {
		--dna-halo-now: var(--dna-halo);
		--dna-glow-now: var(--dna-glow);
	}
</style>
