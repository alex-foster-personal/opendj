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
// Short enough that `.perf-root.deck-layout-less`'s
// `minmax(249px, calc((min(500px, calc(100vh - ...)) + 1px) / 2))` (LESSV-01) resolves its CALC BELOW the
// floor, so the floor is what sizes the row. At STANDARD_VIEWPORT the cap
// governs instead, which is why every existing assertion here has only ever
// measured a mixer with slack. Blinded review, Thu 10 Sep 2026.
const SHORT_VIEWPORT = { width: 1280, height: 560 };
/** `.perf-root.deck-layout-less`'s deck-area floor, derived in
 *  channel-strip-less-floor.test.mjs and written into +page.svelte. */
const LESS_DECK_AREA_FLOOR_PX = 249;

async function enterLessMode(
	page: import('@playwright/test').Page,
	viewport: { width: number; height: number } = STANDARD_VIEWPORT
): Promise<void> {
	await page.setViewportSize(viewport);
	await page.goto('/performance');
	// Scoped and exact: the enrich card's own "Less" (collapse) button also
	// matches a loose name, and appears only while the library is enriching.
	await page.locator('.deck-layout-toggle').getByRole('button', { name: 'LESS', exact: true }).click();
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
test('performance LESS mode: FILTER is visible right of the EQs, fader left, STEM below', async ({
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
				return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
			};
			return {
				fader: pick('.fader-slot'),
				eq: pick('.eq-stack'),
				filter: pick('.filter-slot'),
				stem: pick('.stem-slot')
			};
		});
		expect(boxes.fader, `channel ${deck} fader slot must be present`).not.toBeNull();
		expect(boxes.eq, `channel ${deck} EQ stack must be present`).not.toBeNull();
		expect(boxes.filter, `channel ${deck} FILTER slot must be present`).not.toBeNull();
		expect(boxes.stem, `channel ${deck} STEM slot must be present`).not.toBeNull();
		expect(
			boxes.fader!.right,
			`channel ${deck} fader (right edge ${boxes.fader!.right}) must sit entirely LEFT of the ` +
				`EQ stack (left edge ${boxes.eq!.left}), not stacked above it`
		).toBeLessThanOrEqual(boxes.eq!.left);
		// LESSV-01: FILTER (with TRIM and CUE) sits in the column RIGHT of the
		// EQ stack, and the STEM chips moved to one row UNDER the dial block.
		expect(
			boxes.filter!.left,
			`channel ${deck} FILTER (left edge ${boxes.filter!.left}) must sit RIGHT of the EQ ` +
				`stack (right edge ${boxes.eq!.right})`
		).toBeGreaterThanOrEqual(boxes.eq!.right);
		expect(
			boxes.stem!.top,
			`channel ${deck} STEM controls (top ${boxes.stem!.top}) must sit BELOW the EQ stack ` +
				`(bottom ${boxes.eq!.bottom})`
		).toBeGreaterThanOrEqual(boxes.eq!.bottom);
	}
});

// The floor is the tightest box the mixer ever gets. Since LESSV-01 it is one
// MORE deck tall (249px, with the mixer needing toggle 17 + strip 121 + lower
// 76 + chrome 12 = 226px of it), and at STANDARD_VIEWPORT the row sits right
// on that floor too, so the other tests here now render it as well. This one
// keeps the SHORT_VIEWPORT precondition so a later change that lifts the row
// off the floor fails loudly instead of re-testing slack. The 1px Chromium
// rounding or font-metric drift that #1578 bit on is still what it guards.
// Blinded review, Thu 10 Sep 2026.
//
// It asserts against the STRIP's own box, not the mixer's, and that choice is
// the whole test. Measured at this viewport with the shipped sizes: mixer
// 114-390, strip 138-309, so a strip whose content outgrows its 171px grid row
// does NOT get clipped by anything and does NOT make the mixer grow - the row
// stays 171px and the children simply spill out of it and lie on top of the
// headphone/crossfader row 80px below. Verified by raising LESS_EQ_SIZE from
// 18 to 30: the mixer box, the deck-area box and the lower row are all
// unchanged and still fully inside their parents (EQ 185-308, FILTER 311-338,
// fader to 337, stem to 341, against a strip bottom of 309). Any assertion
// phrased against `.rb-mixer`'s bounds is therefore blind to it.
test('performance LESS mode: at the deck-area FLOOR no strip control spills out of its grid row', async ({
	page
}) => {
	await enterLessMode(page, SHORT_VIEWPORT);

	const deckArea = page.locator('.deck-area');
	await expect(deckArea).toBeVisible();
	const deckAreaBox = await deckArea.boundingBox();
	expect(deckAreaBox, 'the deck area must have a real box').not.toBeNull();
	// Precondition, asserted rather than assumed: this viewport must really
	// have driven the row onto its floor. If a later change raises the cap or
	// the chrome, this fails HERE rather than silently re-testing the slack
	// case that every other test in this file already covers.
	expect(
		deckAreaBox!.height,
		`this viewport must put the deck area ON its ${LESS_DECK_AREA_FLOOR_PX}px floor, ` +
			`not above it (measured ${deckAreaBox!.height})`
	).toBeLessThanOrEqual(LESS_DECK_AREA_FLOOR_PX + 1);
	expect(
		deckAreaBox!.height,
		`the floor must hold the deck area open at ${LESS_DECK_AREA_FLOOR_PX}px ` +
			`(measured ${deckAreaBox!.height})`
	).toBeGreaterThanOrEqual(LESS_DECK_AREA_FLOOR_PX - 1);

	for (const deck of [1, 2]) {
		const strip = page.locator(`[data-mixer-channel="${deck}"]`);
		await expect(strip, `channel ${deck} strip must be present at the floor`).toBeVisible();
		const stripBox = await strip.boundingBox();
		expect(stripBox, `channel ${deck} strip must have a real box`).not.toBeNull();
		const stripBottom = stripBox!.y + stripBox!.height;

		// Every control the LESS grid places, top row to bottom row - including
		// the four (`strip-head`, `cue-btn`, `trim-slot`, `stem-label`) whose
		// placement no rendered test used to observe at all. CSS grid
		// auto-places an unplaced child into the first free cell in DOM order,
		// so losing two `grid-area` declarations at once SWAPS two controls,
		// and the ordering assertions further down are what catch that.
		const controls: Array<[string, string]> = [
			['head', `[data-mixer-channel="${deck}"] .strip-head`],
			['CUE', `[data-mixer-channel="${deck}"] .cue-btn`],
			['TRIM', `[data-knob-id="${deck}:trim"]`],
			['STEM label', `[data-mixer-channel="${deck}"] .stem-label`],
			['HI', `[data-knob-id="${deck}:high"]`],
			['MID', `[data-knob-id="${deck}:mid"]`],
			['LOW', `[data-knob-id="${deck}:low"]`],
			['FILTER', `[data-knob-id="${deck}:filter"]`],
			['fader', `[data-testid="channel-${deck}-fader"]`]
		];
		for (const [name, selector] of controls) {
			const control = page.locator(selector).first();
			await expect(control, `channel ${deck} ${name} must exist at the floor`).toBeAttached();
			const box = await control.boundingBox();
			expect(box, `channel ${deck} ${name} must have a real box at the floor`).not.toBeNull();
			expect(
				box!.height,
				`channel ${deck} ${name} must not be squashed to nothing at the floor`
			).toBeGreaterThan(0);
			expect(
				box!.y + box!.height,
				`channel ${deck} ${name} (bottom edge ${box!.y + box!.height}) has spilled out of ` +
					`its strip (bottom edge ${stripBottom}) at the ${LESS_DECK_AREA_FLOOR_PX}px ` +
					'floor, so it now lies on top of the headphone/crossfader row below'
			).toBeLessThanOrEqual(stripBottom + 1);
		}

		// The grid's own geometry, re-read at the floor: a row that has just
		// run out of height is exactly where a mis-placed child stops being a
		// cosmetic problem and starts overlapping its neighbour.
		const cells = await strip.evaluate((node) => {
			const pick = (selector: string) => {
				const el = node.querySelector(selector);
				if (el === null) return null;
				const r = el.getBoundingClientRect();
				return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
			};
			return {
				cue: pick('.cue-btn'),
				trim: pick('.trim-slot'),
				stemLabel: pick('.stem-label'),
				fader: pick('.fader-slot'),
				eq: pick('.eq-stack'),
				stem: pick('.stem-slot'),
				filter: pick('.filter-slot')
			};
		});
		for (const [name, cell] of Object.entries(cells)) {
			expect(cell, `channel ${deck} ${name} must be present at the floor`).not.toBeNull();
		}
		// LESSV-01 grid: fader | eq | (trim, cue, filter), then one STEM row.
		// The right column is read top to bottom, which is the order a lost
		// `grid-area` would scramble (DOM order is trim, eq, filter, cue).
		expect(cells.fader!.right).toBeLessThanOrEqual(cells.eq!.left);
		for (const [name, cell] of [
			['TRIM', cells.trim!],
			['CUE', cells.cue!],
			['FILTER', cells.filter!]
		] as const) {
			expect(
				cell.left,
				`channel ${deck} ${name} (left ${cell.left}) must sit RIGHT of the EQ stack ` +
					`(right ${cells.eq!.right}) - anything else means a lost grid-area`
			).toBeGreaterThanOrEqual(cells.eq!.right);
		}
		expect(cells.trim!.bottom).toBeLessThanOrEqual(cells.cue!.top);
		expect(cells.cue!.bottom).toBeLessThanOrEqual(cells.filter!.top);
		expect(
			cells.stem!.top,
			`channel ${deck} STEM chips (top ${cells.stem!.top}) belong in the row BELOW the dial ` +
				`block (EQ bottom ${cells.eq!.bottom}, fader bottom ${cells.fader!.bottom})`
		).toBeGreaterThanOrEqual(Math.max(cells.eq!.bottom, cells.fader!.bottom));
		// The STEM mode label sits top right, in the head row above TRIM.
		expect(
			cells.stemLabel!.bottom,
			`channel ${deck} STEM label (bottom ${cells.stemLabel!.bottom}) belongs in the head row ` +
				`ABOVE the dial block (EQ top ${cells.eq!.top})`
		).toBeLessThanOrEqual(cells.eq!.top);
		expect(cells.stemLabel!.left).toBeGreaterThanOrEqual(cells.eq!.right);
	}

	// Second line of defence, cheap: the headphone/crossfader row the strips
	// sit above must still be there and unsquashed at the floor. It does NOT
	// bite on the overflow case above (nothing clips), so it is stated as the
	// separate, weaker check it is rather than being relied on.
	const lower = page.locator('.rb-mixer .lower');
	await expect(lower, 'the mixer lower section must exist at the floor').toBeVisible();
	const lowerBox = await lower.boundingBox();
	expect(lowerBox, 'the mixer lower section must have a real box').not.toBeNull();
	expect(
		lowerBox!.height,
		'the mixer lower section must not be squashed to nothing at the floor'
	).toBeGreaterThan(20);
});

// LESS mounts all four channel strips and hides 3/4 with `opacity: 0` plus
// `pointer-events: none`. Neither removes a descendant from SEQUENTIAL KEYBOARD
// FOCUS, so every control in a collapsed strip was tabbable and arrow-key
// operable while completely invisible - a keyboard user could ride the fader of
// a channel that is not on screen. Mounting FILTER in LESS added two more of
// them, but the hole predates that and covers TRIM, the EQs, CUE, the fader and
// STEM as well; `inert` on the collapsed slot closes all of them at once.
//
// Asserted by actually TRYING to focus each control rather than by reading the
// attribute: `inert` is a thing the browser has to honour, and an attribute
// assertion would stay green on an engine that ignored it.
test('performance LESS mode: no control in a collapsed strip can take focus', async ({ page }) => {
	await enterLessMode(page);

	for (const deck of [3, 4]) {
		const escaped = await page.evaluate((deckId) => {
			const strip = document.querySelector(`[data-mixer-channel="${deckId}"]`);
			if (strip === null) throw new Error(`channel ${deckId} strip is not mounted at all`);
			const slot = strip.closest('.strip-slot');
			if (slot === null) throw new Error(`channel ${deckId} strip has no .strip-slot ancestor`);
			if (!slot.classList.contains('collapsed')) {
				throw new Error(`channel ${deckId} must be COLLAPSED in LESS, or this proves nothing`);
			}
			const focusable = [...slot.querySelectorAll<HTMLElement>('button, input, [tabindex]')];
			if (focusable.length === 0) {
				throw new Error(`channel ${deckId} collapsed slot has no controls - fixture is wrong`);
			}
			const stolen: string[] = [];
			for (const element of focusable) {
				element.focus();
				if (document.activeElement === element) {
					stolen.push(element.getAttribute('data-knob-id') ?? (element.className || element.tagName));
				}
			}
			(document.activeElement as HTMLElement | null)?.blur();
			return { count: focusable.length, stolen };
		}, deck);
		expect(
			escaped.count,
			`channel ${deck} must really mount its controls while collapsed, or this test is vacuous`
		).toBeGreaterThan(3);
		expect(
			escaped.stolen,
			`channel ${deck} is collapsed and invisible, but these controls still took keyboard ` +
				'focus, so a keyboard user can drive an off-screen channel'
		).toEqual([]);
	}

	// Control: the VISIBLE strips must still be fully reachable, so the fix
	// cannot pass by making everything inert.
	for (const deck of [1, 2]) {
		const reachable = await page.evaluate((deckId) => {
			const strip = document.querySelector(`[data-mixer-channel="${deckId}"]`);
			if (strip === null) throw new Error(`channel ${deckId} strip is not mounted`);
			const knob = strip.querySelector<HTMLElement>(`[data-knob-id="${deckId}:filter"]`);
			if (knob === null) throw new Error(`channel ${deckId} has no FILTER knob`);
			knob.focus();
			return document.activeElement === knob || knob.contains(document.activeElement);
		}, deck);
		expect(reachable, `channel ${deck} FILTER must still be keyboard reachable in LESS`).toBe(true);
	}
});

