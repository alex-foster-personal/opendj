import { expect, test } from '@playwright/test';

// Pin 246b0f53f725, follow-on to pin 862cd3 (58a16ac781db) - the maintainer's words:
// "you ddin't move the 1/2 levels so now can't be seen in LESS (requirement
// not to cover required dials etc plz for LESS)."
//
// Pin 862cd3 (LESS mode, see PR #1238) and its follow-on PR #1263 collapse
// decks 3/4 and shrink `.perf-root`'s `deckarea` grid row floor from the
// two-deck-column height (497px) down to the one-deck height (248px) so the
// library gains that space - see +page.svelte's `.perf-root.deck-layout-less`
// rule and library-min-5-rows.test.mjs. `.deck-area`'s grid-template-areas
// is `'decks-left mixer decks-right'` - a SINGLE row - so `<Mixer />` shares
// that same shrunk 248px floor even though pin 862cd3 never collapses decks
// 1/2's mixer channel strips (only their WIDTH shrinks to 0 for strips 3/4;
// height is shared across the whole row). `.rb-mixer` clips overflow, so a
// channel strip whose natural content height exceeds ~248px gets its
// BOTTOM-most elements - the vertical fader and its level meter - clipped
// out of the visible box. That is the defect: "the 1/2 levels" (the mixer
// channel-1/2 faders + their `ChannelLevelMeter`s) were never moved/resized
// when LESS mode shrank the deck-area's available height, so they are no
// longer visible even though decks 1/2 themselves are supposed to stay the
// unclipped, always-visible half of LESS mode.
const STANDARD_VIEWPORT = { width: 1280, height: 800 };

async function enterLessMode(page: import('@playwright/test').Page): Promise<void> {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');
	await page.getByRole('button', { name: 'LESS' }).click();
	await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
	// Scoped to `.rb-deck` specifically (not the bare `[data-deck='3']`
	// attribute selector, which also matches WaveRow's own row div - that
	// row keeps a fixed `height: var(--rb-waverow-h)` on itself and only
	// fades via opacity, so its boundingBox() never collapses and a bare
	// `.first()` there is a false read of "not collapsed yet"). The deck
	// PANEL genuinely collapses via `.deck-col [data-deck='3']`'s max-height
	// rule, which is the actual collapse this test needs to wait for.
	const deck3Panel = page.locator(".rb-deck[data-deck='3']").first();
	await expect
		.poll(async () => (await deck3Panel.boundingBox())?.height ?? -1)
		.toBeLessThanOrEqual(10);
}

test('performance LESS mode: channel 1/2 faders and level meters stay fully inside the visible mixer panel', async ({
	page
}) => {
	await enterLessMode(page);

	const mixer = page.locator('.rb-mixer');
	await expect(mixer).toBeVisible();
	const mixerBox = await mixer.boundingBox();
	expect(mixerBox).not.toBeNull();

	for (const deck of [1, 2]) {
		const fader = page.locator(`[data-testid="channel-${deck}-fader"]`);
		await expect(fader).toBeAttached();
		const faderBox = await fader.boundingBox();
		expect(faderBox, `channel ${deck} fader must have a real box (not display:none)`).not.toBeNull();
		// A clipped-but-present element still returns a bounding box from
		// getBoundingClientRect - it just extends past its overflow:hidden
		// ancestor. Assert it is fully CONTAINED in the mixer's visible box,
		// which `.rb-mixer { overflow: hidden }` enforces - not merely
		// present in the DOM.
		expect(
			faderBox!.y + faderBox!.height,
			`channel ${deck} fader (bottom edge ${faderBox!.y + faderBox!.height}) must not extend ` +
				`past the visible mixer panel (bottom edge ${mixerBox!.y + mixerBox!.height})`
		).toBeLessThanOrEqual(mixerBox!.y + mixerBox!.height + 1);
		expect(faderBox!.height, `channel ${deck} fader must render with real height`).toBeGreaterThan(
			20
		);

		const meter = page.getByRole('meter', { name: new RegExp(`channel ${deck} level`) });
		await expect(meter).toBeAttached();
		const meterBox = await meter.boundingBox();
		expect(meterBox, `channel ${deck} level meter must have a real box`).not.toBeNull();
		expect(
			meterBox!.y + meterBox!.height,
			`channel ${deck} level meter (bottom edge ${meterBox!.y + meterBox!.height}) must not ` +
				`extend past the visible mixer panel (bottom edge ${mixerBox!.y + mixerBox!.height})`
		).toBeLessThanOrEqual(mixerBox!.y + mixerBox!.height + 1);
		expect(
			meterBox!.height,
			`channel ${deck} level meter must render with real height, not be squashed to ~0`
		).toBeGreaterThan(20);

		// Ask 3 ("EQs now don't fit and need adjusting to work") - the HI/MID/
		// LOW knobs above the fader must be fully inside the mixer panel too,
		// not just the fader below them. `.strip > :not(.fader-slot)` carries
		// `flex-shrink: 0` (ChannelStrip.svelte) specifically so a too-short
		// strip overflows visibly here instead of the knobs silently
		// compressing into an overlapping mess that no bounding-box check
		// would ever catch.
		for (const band of ['high', 'mid', 'low']) {
			// knobId(deckId, role) (knob-control.svelte.ts) is `${deckId}:${role}`.
			const knob = page.locator(`[data-knob-id="${deck}:${band}"]`);
			await expect(knob).toBeAttached();
			const knobBox = await knob.boundingBox();
			expect(knobBox, `channel ${deck} ${band} EQ knob must have a real box`).not.toBeNull();
			expect(
				knobBox!.y + knobBox!.height,
				`channel ${deck} ${band} EQ knob (bottom edge ${knobBox!.y + knobBox!.height}) must ` +
					`not extend past the visible mixer panel (bottom edge ${mixerBox!.y + mixerBox!.height})`
			).toBeLessThanOrEqual(mixerBox!.y + mixerBox!.height + 1);
		}

		// STEM mute/solo (StemRow) is the LAST control in the strip's stack -
		// the one most likely to still be pushed off the bottom even once the
		// fader/EQ above it fit, since it has no flex-grow of its own to
		// absorb a too-short strip. Scoped to the MIXER's own strip
		// (`[data-mixer-channel]`), not `.rb-deck` - StemRow is the same
		// component reused on the deck panel itself, which carries an
		// identical aria-label and would otherwise make this locator strict-
		// mode-ambiguous.
		const stems = page
			.locator(`[data-mixer-channel="${deck}"]`)
			.getByRole('group', { name: `stem controls deck ${deck}` });
		await expect(stems).toBeAttached();
		const stemsBox = await stems.boundingBox();
		expect(stemsBox, `channel ${deck} STEM controls must have a real box`).not.toBeNull();
		expect(
			stemsBox!.y + stemsBox!.height,
			`channel ${deck} STEM controls (bottom edge ${stemsBox!.y + stemsBox!.height}) must not ` +
				`extend past the visible mixer panel (bottom edge ${mixerBox!.y + mixerBox!.height})`
		).toBeLessThanOrEqual(mixerBox!.y + mixerBox!.height + 1);
	}
});

// Ask 2 ("MORE/LESS toggle is too big ... pushing EQs down").
test('performance: the MORE/LESS toggle is compact, not oversized chrome pushing the mixer down', async ({
	page
}) => {
	await page.setViewportSize(STANDARD_VIEWPORT);
	await page.goto('/performance');
	const toggle = page.locator('.deck-layout-toggle');
	await expect(toggle).toBeVisible();
	const box = await toggle.boundingBox();
	expect(box).not.toBeNull();
	// Was ~22px (padding-bottom 4 + ~18px buttons) before pin 246b0f5.
	expect(box!.height, `MORE/LESS toggle height (${box!.height}) must be compact`).toBeLessThanOrEqual(
		18
	);
});

// Pin 2917b0eca218 - the maintainer, live on /performance: "in LESS (2 deck) - channel
// slider and butons just below it should probably go Left and Right (around)
// the EQs. Cant currently see filter in LESS mode."
//
// FILTER used to be unmounted in LESS (`{#if !less}`) because a single flex
// COLUMN could not afford its height inside the shrunk deck-area row.
// `.strip.less` is now a three-column grid, so only the dial column has to be
// tall. Both halves of the ask are asserted from RENDERED geometry rather than
// from the stylesheet: a `grid-template-areas` string proves intent, a
// bounding box proves the pixels.
test('performance LESS mode: FILTER is visible, with the fader left of the EQs and STEM right', async ({
	page
}) => {
	await enterLessMode(page);

	const mixer = page.locator('.rb-mixer');
	await expect(mixer).toBeVisible();
	const mixerBox = await mixer.boundingBox();
	expect(mixerBox).not.toBeNull();

	for (const deck of [1, 2]) {
		// knobId(deckId, role) (knob-control.svelte.ts) is `${deckId}:${role}`.
		const filter = page.locator(`[data-knob-id="${deck}:filter"]`);
		await expect(filter, `channel ${deck} FILTER must exist in LESS mode`).toBeVisible();
		const filterBox = await filter.boundingBox();
		expect(filterBox, `channel ${deck} FILTER must have a real box`).not.toBeNull();
		expect(filterBox!.height, `channel ${deck} FILTER must render at a real size`).toBeGreaterThan(
			10
		);
		// Present is not the same as SEEN: `.rb-mixer { overflow: hidden }`
		// would clip a FILTER that had been given back its markup but no room,
		// and getBoundingClientRect reports the clipped element's full box.
		expect(
			filterBox!.y + filterBox!.height,
			`channel ${deck} FILTER (bottom edge ${filterBox!.y + filterBox!.height}) must not ` +
				`extend past the visible mixer panel (bottom edge ${mixerBox!.y + mixerBox!.height})`
		).toBeLessThanOrEqual(mixerBox!.y + mixerBox!.height + 1);

		const strip = page.locator(`[data-mixer-channel="${deck}"]`);
		const boxes = await strip.evaluate((node) => {
			const pick = (selector: string) => {
				const el = node.querySelector(selector);
				if (el === null) return null;
				const r = el.getBoundingClientRect();
				return { left: r.left, right: r.right };
			};
			return { fader: pick('.fader-slot'), eq: pick('.eq-stack'), stem: pick('.stem-slot') };
		});
		expect(boxes.fader, `channel ${deck} fader slot must be present`).not.toBeNull();
		expect(boxes.eq, `channel ${deck} EQ stack must be present`).not.toBeNull();
		expect(boxes.stem, `channel ${deck} STEM slot must be present`).not.toBeNull();
		expect(
			boxes.fader!.right,
			`channel ${deck} fader (right edge ${boxes.fader!.right}) must sit entirely LEFT of the ` +
				`EQ stack (left edge ${boxes.eq!.left}), not stacked above it`
		).toBeLessThanOrEqual(boxes.eq!.left);
		expect(
			boxes.stem!.left,
			`channel ${deck} STEM controls (left edge ${boxes.stem!.left}) must sit entirely RIGHT ` +
				`of the EQ stack (right edge ${boxes.eq!.right})`
		).toBeGreaterThanOrEqual(boxes.eq!.right);
	}
});
