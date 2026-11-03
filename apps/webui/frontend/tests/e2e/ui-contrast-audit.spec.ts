import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';

import { waitForPerformanceIpc } from './support/performance-ready';
import { appendEmptyRescueSnapshot } from './support/rescue-snapshot';
import { applySkin, expectSkin, restoreThemeAfterEach, SKINS, type Skin } from './support/skin-prefs';
import {
	renderedContrastAudit,
	trackCanvasText,
	type ContrastFinding,
	type RenderedContrastReport,
} from './support/rendered-contrast';

// requirement: UICONTRAST-01 (orders board W34; the maintainer, Tue 6 Oct 2026: "MORE |
// LESS and crossfader number selectors on gothic the white on light gray
// should be black font - basic contrast rules test should have caught this")
// [if] any visible text on /performance (MORE, LESS, and every control forced
// to :hover), the library or Settings, in the Default, Gothic or Light skin
// on the dark theme or the Default skin on the light theme ('theme-light'),
// including form-control values, placeholders and CSS-generated labels,
// renders below 4.5:1 against its composited backdrop (3:1 for large text)
// [then] the run fails, unless an allowlist entry names it with a reason
// [else stop]. [if] an allowlist entry matches nothing in a skin it lists
// [then] the run fails as stale [else stop]. The token-pair tests in
// tests/unit/color-contrast.test.mjs check the colors a skin declares; this
// checks what the browser paints, which is where Gothic's white-on-#d6d6d6
// MORE button (1.45:1) hid.

type Surface = 'performance-more' | 'performance-less' | 'performance-hover' | 'library' | 'settings';

interface AllowEntry {
	el: string;
	skins: Skin[];
	surfaces: Surface[];
	reason: string;
	/** Volatile render state: matched when present, never reported stale. */
	optional?: boolean;
}

const ALLOW: { findings: AllowEntry[]; unmeasured: AllowEntry[] } = JSON.parse(
	readFileSync(fileURLToPath(new URL('./ui-contrast-audit.allow.json', import.meta.url)), 'utf8')
);

/** The shell background each pass must paint: the light theme and, since
 * #5812, the Light skin restyle the library and Settings shell too. */
const SHELL_BG: Record<Skin, string> = {
	default: 'rgb(11, 13, 17)',
	'mono-dev': 'rgb(11, 13, 17)',
	light: 'rgb(245, 245, 245)',
	'theme-light': 'rgb(247, 243, 235)'
};

test.beforeEach(async ({ page }) => {
	await page.addInitScript(trackCanvasText);
});
restoreThemeAfterEach();

async function _audit(page: Page): Promise<RenderedContrastReport> {
	return page.evaluate(renderedContrastAudit, undefined);
}

const HOVER_OWNER = 'data-contrast-hover-owner';

/**
 * Mark every element a stylesheet `:hover` rule hangs on. forcePseudoState
 * forces only the node it is given, so `.cand:hover .play-btn` changes the
 * button only when the `.cand` li itself is forced. The owner is the part of
 * each selector before `:hover`, read from the live stylesheets.
 */
function _markHoverOwners(attr: string): { marked: number; unreadable: string[]; invalid: string[] } {
	const owners = new Set<string>();
	// Split a selector list on its top-level commas only: the comma inside
	// `html:is(.a, .b) img:hover` belongs to `:is()`, not to the list.
	const splitList = (text: string): string[] => {
		const parts: string[] = [];
		let depth = 0;
		let quote = '';
		let start = 0;
		for (let i = 0; i < text.length; i++) {
			const ch = text[i];
			if (quote) {
				if (ch === '\\') i++;
				else if (ch === quote) quote = '';
			} else if (ch === '"' || ch === "'") quote = ch;
			else if (ch === '(' || ch === '[') depth++;
			else if (ch === ')' || ch === ']') depth--;
			else if (ch === ',' && depth === 0) {
				parts.push(text.slice(start, i));
				start = i + 1;
			}
		}
		parts.push(text.slice(start));
		return parts;
	};
	const walk = (rules: CSSRuleList) => {
		for (const rule of Array.from(rules)) {
			if (rule instanceof CSSStyleRule) {
				for (const part of splitList(rule.selectorText)) {
					const at = part.indexOf(':hover');
					if (at < 0) continue;
					// `.a:is(.b:hover)` cuts inside the parentheses; CSS closes an
					// open block at the end of a selector, so `.a:is(.b` parses.
					const owner = part.slice(0, at).trim();
					// A bare `:hover` names no owner to force: fail it, never drop it.
					owners.add(owner || ':hover (no owner)');
					// Inside a function the hovered element is the inner subject,
					// not the outer one: `tr:has(.c-art:hover)` hovers `.c-art`, so
					// force the compound after the innermost open parenthesis too.
					let depth = 0;
					let open = -1;
					for (let i = owner.length - 1; i >= 0; i--) {
						if (owner[i] === ')') depth++;
						else if (owner[i] === '(' && depth-- === 0) {
							open = i;
							break;
						}
					}
					const inner = open < 0 ? '' : owner.slice(open + 1).trim();
					if (inner) owners.add(inner);
				}
			}
			if ('cssRules' in rule) walk((rule as CSSGroupingRule).cssRules);
		}
	};
	const unreadable: string[] = [];
	for (const sheet of Array.from(document.styleSheets)) {
		let rules: CSSRuleList;
		try {
			rules = sheet.cssRules;
		} catch {
			// An unreadable sheet's hover rules would go unforced: report it.
			unreadable.push(sheet.href ?? '(inline)');
			continue;
		}
		walk(rules);
	}
	let marked = 0;
	const invalid: string[] = [];
	for (const sel of owners) {
		let els: Element[];
		try {
			els = Array.from(document.querySelectorAll(sel));
		} catch {
			invalid.push(sel);
			continue;
		}
		for (const el of els) {
			if (!el.hasAttribute(attr)) marked++;
			el.setAttribute(attr, '');
		}
	}
	return { marked, unreadable, invalid };
}

/** Force :hover on every interactive element and every hover-rule owner, through CDP. */
async function _forceHover(page: Page, on: boolean): Promise<void> {
	if (on) {
		const owners = await page.evaluate(_markHoverOwners, HOVER_OWNER);
		expect(owners.unreadable, 'stylesheets whose :hover rules cannot be read').toEqual([]);
		expect(owners.invalid, ':hover owner selectors that did not parse').toEqual([]);
		expect(owners.marked, 'no element owns a :hover rule, so the stylesheet read is broken').toBeGreaterThan(0);
	}
	const cdp = await page.context().newCDPSession(page);
	await cdp.send('DOM.enable');
	await cdp.send('CSS.enable');
	const { root } = await cdp.send('DOM.getDocument', { depth: -1 });
	const { nodeIds } = await cdp.send('DOM.querySelectorAll', {
		nodeId: root.nodeId,
		selector: `button, a, [role="button"], tr, .row, .cell, label, [${HOVER_OWNER}]`,
	});
	for (const nodeId of nodeIds) {
		try {
			await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: on ? ['hover'] : [] });
		} catch (exc) {
			// A live re-render can remove a node between the query and this
			// call; a node that no longer exists has nothing to hover. Any
			// other protocol error still fails the run.
			if (!String(exc).includes('Could not find node with given id')) throw exc;
		}
	}
	if (!on) await cdp.detach();
	await page.waitForTimeout(200);
}

async function _loadTwoDecks(page: Page): Promise<void> {
	const tracks: string[] = await page.evaluate(async () => {
		const res = await fetch('/api/v1/tracks?limit=2');
		return (await res.json()).items.map((t: { stable_id: string }) => t.stable_id);
	});
	expect(tracks.length, 'the fixture library has two tracks').toBeGreaterThanOrEqual(2);
	for (const [index, stable_id] of tracks.entries()) {
		await page.evaluate(
			([deck, sid]) => window.musicDjToolsPerformance!.dispatch({ type: 'load', deck, stable_id: sid }),
			[index + 1, stable_id] as const
		);
	}
	// A loading deck paints a sweep (`.rb-deck.loading::after`) over its whole
	// panel, which the audit reports as unmeasured; audit the settled decks.
	await expect(page.locator('section.rb-deck.loading')).toHaveCount(0, { timeout: 30_000 });
}

async function _setDeckLayout(page: Page, layout: 'MORE' | 'LESS'): Promise<void> {
	const btn = page.locator('button.deck-layout-btn', { hasText: layout });
	await expect(btn).toBeVisible();
	await expect(async () => {
		await btn.click();
		await expect(page.locator('button.deck-layout-btn.active', { hasText: layout })).toBeVisible();
		if (layout === 'LESS') {
			await expect(page.locator('.perf-root')).toHaveClass(/deck-layout-less/);
		} else {
			await expect(page.locator('.perf-root')).not.toHaveClass(/deck-layout-less/);
		}
	}).toPass({ timeout: 15_000 });
}

/**
 * Positive control on the instrument (CORE on #5727): the fewest text
 * elements each surface measured in any skin on Wed 7 Oct 2026 with the
 * fixture library, less a margin for deck-state noise. A surface that falls
 * below its floor measured less than it should, so the run fails rather than
 * passing on what it skipped.
 */
const MEASURED_FLOOR: Record<Surface, number> = {
	'performance-more': 210,
	'performance-less': 144,
	'performance-hover': 245,
	library: 30,
	settings: 96,
};

const PLANTED = 'planted-bad-pair';

/**
 * The audit's own name for a backdrop that is a deck's loading sweep, e.g.
 * "pseudo-element fill on deck-col > section.rb-deck.rb-panel.loading.selected::after".
 */
const DECK_LOADING_SWEEP = /section\.rb-deck[\w.-]*\.loading[\w.-]*::after/;

/** Audit with one known-bad pair planted (#777777 on #888888); it must be flagged. */
async function _auditPlantedOnce(page: Page): Promise<RenderedContrastReport> {
	await page.evaluate((text) => {
		const el = document.createElement('div');
		el.id = 'contrast-planted';
		el.textContent = text;
		el.style.cssText =
			'position:fixed;right:0;bottom:0;z-index:2147483647;background:#888888;color:#777777;font-size:12px;padding:2px';
		document.body.appendChild(el);
	}, PLANTED);
	const report = await _audit(page);
	await page.evaluate(() => document.getElementById('contrast-planted')?.remove());
	return report;
}

/**
 * Sample the settled surface. A deck paints its loading sweep
 * (`.rb-deck.loading::after`, which covers the whole panel) while ANY command
 * is in flight on it, so it can re-enter the sweep after a layout switch, a
 * skin change, or a late follow-up to a load. Waiting for the sweep to clear
 * and then sampling are separate round trips, and a command that starts
 * between them puts the sweep back under the sample (issue #6101: 30
 * unmeasured entries on default/performance-more). So the wait and the sample
 * are one retried step: a sample that saw the sweep was taken mid-load and is
 * discarded, never recorded and never excused. A sweep that does not clear
 * fails here with the elements it covered.
 */
async function _auditPlanted(page: Page): Promise<RenderedContrastReport> {
	let settled: RenderedContrastReport | null = null;
	await expect(async () => {
		await expect(page.locator('section.rb-deck.loading')).toHaveCount(0, { timeout: 30_000 });
		const report = await _auditPlantedOnce(page);
		const midLoad = report.unmeasured.filter((u) => DECK_LOADING_SWEEP.test(u.why));
		expect(
			midLoad.map((u) => `${u.el}: ${u.why}`),
			'text sampled under a deck loading sweep'
		).toEqual([]);
		settled = report;
	}).toPass({ timeout: 90_000 });
	if (settled === null) throw new Error('the contrast audit never produced a settled sample');
	return settled;
}

const key = (skin: Skin, surface: Surface, el: string) => `${skin} | ${surface} | ${el}`;

function _allowed(list: AllowEntry[], skin: Skin, surface: Surface, el: string): AllowEntry | undefined {
	return list.find((a) => a.el === el && a.skins.includes(skin) && a.surfaces.includes(surface));
}

test('every visible text element clears WCAG AA in every skin, or is allowlisted with a reason', async ({ page }) => {
	test.setTimeout(240_000);
	await page.setViewportSize({ width: 1600, height: 900 });

	const unexpected: string[] = [];
	const unmeasuredUnexpected: string[] = [];
	const hit = new Set<string>();
	const table: string[] = [];
	let measured = 0;
	const counts: string[] = [];

	const record = (skin: Skin, surface: Surface, report: RenderedContrastReport) => {
		measured += report.measured;
		counts.push(`${skin} | ${surface} | ${report.measured} measured, ${report.findings.length} below AA (planted pair included)`);
		expect(report.measured, `${skin}/${surface} measured fewer elements than its floor`).toBeGreaterThanOrEqual(
			MEASURED_FLOOR[surface]
		);
		const planted = report.findings.filter((f) => f.text === PLANTED);
		expect(planted.map((f) => f.ratio), `${skin}/${surface}: the planted #777 on #888 pair was not flagged`).toEqual([1.26]);
		report = { ...report, findings: report.findings.filter((f) => f.text !== PLANTED) };
		const dedup = new Map<string, ContrastFinding>();
		for (const f of report.findings) if (!dedup.has(f.el) || dedup.get(f.el)!.ratio > f.ratio) dedup.set(f.el, f);
		for (const f of dedup.values()) {
			const allow = _allowed(ALLOW.findings, skin, surface, f.el);
			table.push(`${allow ? 'allowed' : 'NEW'} | ${skin} | ${surface} | ${f.el} | "${f.text}" | ${f.fg} on ${f.bg} | ${f.ratio}:1 < ${f.need}:1`);
			if (allow) hit.add(`F ${skin} ${allow.el}`);
			else unexpected.push(`${key(skin, surface, f.el)} "${f.text}" ${f.fg} on ${f.bg} = ${f.ratio}:1 (needs ${f.need}:1)`);
		}
		for (const u of report.unmeasured) {
			// SVG text (fill over shapes) is out of this instrument's reach, so each
			// one must be named in the unmeasured allowlist, never passed silently.
			const allow = _allowed(ALLOW.unmeasured, skin, surface, u.el);
			if (allow) hit.add(`U ${skin} ${allow.el}`);
			else unmeasuredUnexpected.push(`${key(skin, surface, u.el)}: ${u.why}`);
		}
	};

	for (const skin of SKINS) {
		// The page's rescue auto-restore (Gig posture, which earlier specs
		// leave on) writes the newest snapshot's deck_layout once this tab
		// wins leadership. A writer snapshot names the layout of the last
		// load, so a restore landing after the LESS click below put MORE back
		// (run 38673864231). Pin a snapshot with no layout first, while no
		// /performance page (and so no ring writer) is open: the previous
		// iteration ended on /settings, the first one on a blank page.
		expect(new URL(page.url()).pathname, 'a /performance page is still open to write a newer snapshot').not.toBe(
			'/performance'
		);
		await appendEmptyRescueSnapshot(page.request);
		await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
		await waitForPerformanceIpc(page);
		await applySkin(page, skin);
		await _loadTwoDecks(page);
		for (const layout of ['MORE', 'LESS'] as const) {
			await _setDeckLayout(page, layout);
			record(skin, layout === 'MORE' ? 'performance-more' : 'performance-less', await _auditPlanted(page));
		}
		await _setDeckLayout(page, 'MORE');
		await _forceHover(page, true);
		record(skin, 'performance-hover', await _auditPlanted(page));
		await _forceHover(page, false);

		for (const [route, surface] of [['/', 'library'], ['/settings', 'settings']] as const) {
			await page.goto(route, { waitUntil: 'domcontentloaded' });
			await page.waitForLoadState('networkidle').catch(() => {});
			await expectSkin(page, skin);
			// Control: the theme really reached the shell, not just the attribute.
			const shell = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
			expect(shell, `${skin}/${surface} shell background`).toBe(SHELL_BG[skin]);
			record(skin, surface, await _auditPlanted(page));
		}
	}

	console.log(`[UICONTRAST-01] measured per skin and surface:\n${counts.join('\n')}`);
	console.log(`[UICONTRAST-01] measured ${measured} text elements; findings table:\n${table.join('\n')}`);
	const stale: string[] = [];
	for (const [kind, list] of [['F', ALLOW.findings], ['U', ALLOW.unmeasured]] as const) {
		for (const a of list) {
			expect(a.reason.trim().length, `allowlist entry ${a.el} has no reason`).toBeGreaterThan(10);
			if (a.optional) continue;
			for (const skin of a.skins) if (!hit.has(`${kind} ${skin} ${a.el}`)) stale.push(`${kind} ${skin} | ${a.el}`);
		}
	}
	expect(unmeasuredUnexpected, 'text this audit could not measure (gradient or unparsed backdrop)').toEqual([]);
	expect(unexpected, 'text below WCAG AA that no allowlist entry names').toEqual([]);
	expect(stale, 'allowlist entries that matched nothing in a skin they list: remove them').toEqual([]);
});

test('controls: the audit fires on Gothic white-on-accent, and composites a translucent fill', async ({ page }) => {
	// _applySkin waits for this page to lead the UI mirror, which can take one
	// 10 s lease expiry when the pre-reload page wins its PUT/DELETE race, and
	// the audits below run in sequence. The root config's 30 s cap was hit on
	// main run 38836701046 (Fri 16 Oct 2026) before this wait existed.
	test.setTimeout(90_000);
	await page.setViewportSize({ width: 1600, height: 900 });
	await page.goto('/performance?muted=1', { waitUntil: 'domcontentloaded' });
	await waitForPerformanceIpc(page);
	await applySkin(page, 'mono-dev');
	await _setDeckLayout(page, 'MORE');

	// Positive control derived from the W34 defect itself: restore the
	// pre-fix white label on Gothic's #d6d6d6 accent; the audit must report it.
	const style = await page.addStyleTag({ content: '.deck-layout-btn.active { color: #fff !important; }' });
	const broken = await _audit(page);
	const more = broken.findings.find((f) => f.el.endsWith('button.deck-layout-btn.active'));
	expect(more, 'the audit missed white text on the Gothic accent').toBeTruthy();
	expect(more!.ratio).toBeLessThan(1.6);
	await style.evaluate((node) => (node as Element).remove());
	const fixed = await _audit(page);
	expect(fixed.findings.some((f) => f.el.endsWith('button.deck-layout-btn.active'))).toBe(false);

	// Compositing control: light text on a 10% white tint over a near-black
	// panel is ~11:1. A naive check that ignores alpha reads the tint as
	// white and flags it; this audit must measure the composited gray.
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:0;top:0;z-index:99999;background:#101010;padding:4px';
		host.innerHTML = '<span style="background:rgba(255,255,255,0.1);color:#e0e0e0;font-size:12px">probe</span>';
		document.body.appendChild(host);
	});
	const probe = (await _audit(page)).probes.find((p) => p.text === 'probe');
	expect(probe, 'the probe was not measured').toBeTruthy();
	expect(probe!.bg).toBe('#282828');
	expect(probe!.ratio).toBeGreaterThan(4.5);

	// Opacity control (Codex on #5727): #838990 on #14171d is 5.08:1 as
	// declared, but at opacity 0.6 it paints about #575b62, which fails.
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:0;top:40px;z-index:99999;background:#14171d;padding:4px';
		host.innerHTML = '<span style="opacity:0.6;color:#838990;font-size:12px">faded</span>';
		document.body.appendChild(host);
	});
	const faded = (await _audit(page)).probes.find((p) => p.text === 'faded');
	expect(faded, 'the faded probe was not measured').toBeTruthy();
	expect(faded!.ratio, 'opacity was not applied to the painted color').toBeLessThan(3);

	// Rounding control (#6082): raw ~4.495 paints as 4.5:1 and must not fail a 4.5 floor.
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:0;top:80px;z-index:99999;background:#14171d;padding:4px';
		host.innerHTML = '<span style="color:#7a8087;font-size:12px">round</span>';
		document.body.appendChild(host);
	});
	const roundReport = await _audit(page);
	const round = roundReport.probes.find((p) => p.text === 'round');
	expect(round, 'the rounding probe was not measured').toBeTruthy();
	expect(round!.ratio).toBe(4.5);
	expect(roundReport.findings.some((f) => f.text === 'round')).toBe(false);

	// Group-opacity control (Codex on #5727): opacity on an element with its
	// own opaque fill fades the fill too. Black on white at 0.6 over black
	// paints the fill #999999; fading only the text would keep it #ffffff.
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:200px;top:40px;z-index:99999;background:#000000;padding:4px';
		host.innerHTML = '<span style="opacity:0.6;background:#ffffff;color:#000000;font-size:12px">grp</span>';
		document.body.appendChild(host);
	});
	const grp = (await _audit(page)).probes.find((p) => p.text === 'grp');
	expect(grp, 'the group probe was not measured').toBeTruthy();
	expect(grp!.bg, 'a faded element\'s own fill was not faded with it').toBe('#999999');
	expect(grp!.fg).toBe('#000000');

	// Hover-owner control (Codex on #5727): the rule hangs on an li, which the
	// interactive-element selector does not name, so only reading the owner
	// from the stylesheet forces it.
	// The second rule's :hover sits inside :is(), so its owner prefix is cut
	// mid-parentheses (Sol on #5727: an unparsed owner now fails the run).
	await page.addStyleTag({
		content:
			'.hover-probe-text { color: #e0e0e0; } .hover-probe-owner:hover .hover-probe-text { color: #3a3a3a; }' +
			' .hover-probe-is:is(.hover-probe-in:hover) .hover-probe-text { color: #3a3a3a; }' +
			' .hover-probe-has:has(.hover-probe-sub:hover) .hover-probe-text { color: #3a3a3a; }',
	});
	await page.evaluate(() => {
		const host = document.createElement('ul');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:0;top:80px;z-index:99999;background:#101010;padding:4px;margin:0;list-style:none';
		host.innerHTML =
			'<li class="hover-probe-owner"><span class="hover-probe-text" style="font-size:12px">owned</span></li>' +
			'<li class="hover-probe-is hover-probe-in"><span class="hover-probe-text" style="font-size:12px">inner</span></li>' +
			'<li class="hover-probe-has"><i class="hover-probe-sub"></i><span class="hover-probe-text" style="font-size:12px">hassub</span></li>';
		document.body.appendChild(host);
	});
	const owned = async (text: string) => (await _audit(page)).probes.find((p) => p.text === text);
	expect((await owned('owned'))!.ratio, 'the hover probe fails before any hover').toBeGreaterThan(4.5);
	expect((await owned('inner'))!.ratio, 'the :is() hover probe fails before any hover').toBeGreaterThan(4.5);
	expect((await owned('hassub'))!.ratio, 'the :has() hover probe fails before any hover').toBeGreaterThan(4.5);
	await _forceHover(page, true);
	expect((await owned('owned'))!.ratio, 'forcing hover missed a rule owned by a non-interactive ancestor').toBeLessThan(2);
	expect((await owned('inner'))!.ratio, 'forcing hover missed a rule whose :hover sits inside :is()').toBeLessThan(2);
	// The subject of `:has(.sub:hover)` is a child the outer element only
	// contains, so forcing the outer element alone leaves this rule off.
	expect((await owned('hassub'))!.ratio, 'forcing hover missed the inner subject of :has(:hover)').toBeLessThan(2);
	await _forceHover(page, false);

	// Fail-loud control (Sol on #5727): a hover rule with no owner to force,
	// and a sheet whose rules cannot be read, are reported, never dropped.
	await page.addStyleTag({ content: ':hover { outline-offset: 0; }' });
	const foreign = 'http://contrast-probe.invalid/sheet.css';
	await page.route(foreign, (r) => r.fulfill({ contentType: 'text/css', body: '.x:hover { color: red; }' }));
	await page.evaluate(
		(href) =>
			new Promise((done) => {
				const link = document.createElement('link');
				link.rel = 'stylesheet';
				link.href = href;
				link.onload = link.onerror = done;
				document.head.append(link);
			}),
		foreign
	);
	// A comma inside :is() is not a list separator (theme.css's artwork rule
	// `html:is([data-skin=...], [data-skin=...]) img:hover`): the owner is the
	// whole compound, so the element is marked and nothing is reported invalid.
	await page.addStyleTag({ content: 'html:is(:root, .probe-is-b) .probe-is-owner:hover { color: #333333; }' });
	await page.evaluate(() => {
		const el = document.createElement('div');
		el.className = 'probe-is-owner';
		el.textContent = 'IS';
		document.body.append(el);
	});
	const loud = await page.evaluate(_markHoverOwners, HOVER_OWNER);
	expect(loud.invalid, 'a bare :hover rule was dropped').toContain(':hover (no owner)');
	expect(loud.unreadable, 'an unreadable stylesheet was skipped').toEqual([foreign]);
	expect(loud.invalid.filter((s) => s.includes('probe-is')), 'a comma inside :is() split the selector').toEqual([]);
	expect(
		await page.locator(`.probe-is-owner[${HOVER_OWNER}]`).count(),
		'the :is() hover owner was not marked'
	).toBe(1);
	await page.evaluate(() => document.querySelector('.probe-is-owner')?.remove());
	await page.evaluate((href) => {
		document.head.querySelector(`link[href="${href}"]`)?.remove();
		for (const s of document.head.querySelectorAll('style')) if (s.textContent?.startsWith(':hover {')) s.remove();
	}, foreign);

	// Non-DOM text control (Codex on #5727): a placeholder and a CSS-generated
	// label paint text with no text node; both must be measured, dark on dark.
	await page.addStyleTag({
		content:
			'.probe-ph::placeholder { color: #333333; } .probe-gen::after { content: "GEN"; color: #333333; font-size: 12px; }',
	});
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:0;top:120px;z-index:99999;background:#101010;padding:4px';
		host.innerHTML =
			'<input class="probe-ph" placeholder="hint" style="background:#101010;border:0;font-size:12px"><span class="probe-gen"></span>';
		document.body.appendChild(host);
	});
	const nonDom = (await _audit(page)).probes;
	const ph = nonDom.find((p) => p.text === 'hint');
	expect(ph, 'the placeholder was not measured').toBeTruthy();
	expect(ph!.el.endsWith('::placeholder')).toBe(true);
	expect(ph!.ratio).toBeLessThan(2);
	const gen = nonDom.find((p) => p.text === 'GEN');
	expect(gen, 'the generated label was not measured').toBeTruthy();
	expect(gen!.ratio).toBeLessThan(2);

	// Overlapping-sibling control (Codex and Sol on #5727, StemsProgress's
	// layout): white text over a white positioned fill that is its SIBLING,
	// not its ancestor, inside a dark panel, with the fill click-through.
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:0;top:160px;z-index:99999;background:#101010;width:120px;height:20px';
		host.innerHTML =
			'<div style="position:absolute;inset:0;background:#ffffff;pointer-events:none"></div>' +
			'<span style="position:relative;color:#ffffff;font-size:12px">over</span>';
		document.body.appendChild(host);
	});
	const over = (await _audit(page)).probes.find((p) => p.text === 'over');
	expect(over, 'the overlap probe was not measured').toBeTruthy();
	expect(over!.bg, 'the sibling fill under the text was not composited').toBe('#ffffff');
	expect(over!.ratio).toBeLessThan(1.5);

	// Backdrops with no CSS color (Codex on #5727): text over a sibling <img>,
	// and text over its row's positioned ::before gradient, must come back
	// unmeasured, not measured against the dark panel behind them.
	await page.addStyleTag({
		content:
			'.probe-row { position: relative; } .probe-row::before { content: ""; position: absolute; inset: 0; background: linear-gradient(#fff, #eee); }',
	});
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:0;top:200px;z-index:99999;background:#101010;width:160px';
		host.innerHTML =
			'<div class="probe-img" style="position:relative;height:20px">' +
			'<img alt="" style="position:absolute;inset:0;width:100%;height:100%" src="data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==">' +
			'<span class="probe-on-img" style="position:relative;color:#ffffff;font-size:12px">onimg</span></div>' +
			'<div class="probe-row" style="height:20px"><span class="probe-on-pseudo" style="position:relative;color:#ffffff;font-size:12px">onpseudo</span></div>';
		document.body.appendChild(host);
	});
	const backdrops = await _audit(page);
	for (const [cls, why] of [
		['span.probe-on-img', 'replaced element'],
		['span.probe-on-pseudo', 'pseudo-element fill'],
	] as const) {
		const u = backdrops.unmeasured.find((x) => x.el.endsWith(cls));
		expect(u?.why, `${cls} was measured against the panel instead of reported unmeasured`).toContain(why);
		expect(backdrops.probes.some((p) => p.el.endsWith(cls))).toBe(false);
	}

	// Edge-ring control (PAIR-09): a row's ::after fill masked to thin strips
	// along its edges paints nothing under text in the middle of the row, so
	// that text is measured; the same fill masked with strips thick enough to
	// reach the text stays unmeasured.
	await page.addStyleTag({
		content:
			'.probe-ring, .probe-thick { position: relative; } ' +
			'.probe-ring::after, .probe-thick::after { content: ""; position: absolute; inset: 0; background: #ffffff; ' +
			'mask-image: linear-gradient(#000, #000), linear-gradient(#000, #000); mask-repeat: repeat-x, repeat-y; } ' +
			'.probe-ring::after { mask-size: 10px 3px, 3px 10px; } .probe-thick::after { mask-size: 10px 30px, 30px 10px; }',
	});
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:700px;top:40px;z-index:99999;background:#101010;width:200px';
		host.innerHTML =
			'<div class="probe-ring" style="height:24px;padding:4px 12px"><span style="position:relative;color:#333333;font-size:12px">ringtext</span></div>' +
			'<div class="probe-thick" style="height:24px;padding:4px 12px"><span class="probe-in-thick" style="position:relative;color:#333333;font-size:12px">thicktext</span></div>';
		document.body.appendChild(host);
	});
	const rings = await _audit(page);
	const ring = rings.probes.find((p) => p.text === 'ringtext');
	expect(ring, 'text inside an edge-ring pseudo was not measured').toBeTruthy();
	expect(ring!.ratio).toBeLessThan(2);
	expect(
		rings.unmeasured.find((u) => u.el.endsWith('span.probe-in-thick'))?.why,
		'text under a thick masked fill was measured instead of reported unmeasured'
	).toContain('pseudo-element fill');

	// Inert styling and filters (Codex on #5727): a status row that only
	// carries the app's inert class is text and must be measured, while the
	// same class on a control exempts it; text under a color-changing filter
	// is unmeasured, and a drop-shadow-only filter does not hide it.
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:0;top:240px;z-index:99999;background:#101010;width:200px';
		const dim = 'color:#222222;font-size:12px';
		host.innerHTML =
			`<div class="rb-inert" style="${dim}">inertrow</div>` +
			`<button class="rb-inert" style="${dim};background:#101010;border:0">inertbtn</button>` +
			`<div style="filter:grayscale(1)"><span class="probe-filtered" style="${dim}">filtered</span></div>` +
			`<div style="filter:drop-shadow(0 1px 2px rgb(0 0 0 / 0.5))"><span style="${dim}">shadowed</span></div>`;
		document.body.appendChild(host);
	});
	const states = await _audit(page);
	const byText = (t: string) => states.probes.find((p) => p.text === t);
	expect(byText('inertrow'), 'a status row with the inert class was exempted as a control').toBeTruthy();
	expect(byText('inertrow')!.ratio).toBeLessThan(1.5);
	expect(byText('inertbtn'), 'an inert-class control was measured').toBeFalsy();
	expect(
		states.unmeasured.find((u) => u.el.endsWith('span.probe-filtered'))?.why,
		'filtered text was not reported unmeasured'
	).toContain('css filter');
	expect(byText('filtered'), 'filtered text was measured from its pre-filter colors').toBeFalsy();
	expect(byText('shadowed'), 'a drop-shadow-only filter hid the text').toBeTruthy();

	// Canvas-painted text (Codex on #5727, the waveform split-row labels): a
	// canvas that drew text is reported unmeasured; one that only drew shapes
	// is not.
	await page.evaluate(() => {
		const host = document.createElement('div');
		host.setAttribute('data-contrast-probe', '');
		host.style.cssText = 'position:fixed;left:0;top:320px;z-index:99999';
		for (const [cls, text] of [
			['probe-canvas-text', true],
			['probe-canvas-shape', false],
		] as const) {
			const c = document.createElement('canvas');
			c.className = cls;
			c.width = 80;
			c.height = 20;
			const ctx = c.getContext('2d')!;
			if (text) ctx.fillText('LABEL', 2, 14);
			else ctx.fillRect(0, 0, 10, 10);
			host.appendChild(c);
		}
		document.body.appendChild(host);
	});
	const canvases = (await _audit(page)).unmeasured;
	expect(
		canvases.find((u) => u.el.endsWith('canvas.probe-canvas-text'))?.why,
		'canvas text was skipped instead of reported unmeasured'
	).toContain('canvas-painted text');
	expect(canvases.some((u) => u.el.endsWith('canvas.probe-canvas-shape'))).toBe(false);
	expect(canvases.some((u) => u.why.includes('tracker not installed'))).toBe(false);
});
