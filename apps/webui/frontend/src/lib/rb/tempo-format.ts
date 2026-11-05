/** DECKUX-41 (W35, the maintainer Tue 6 Oct 2026: "should be +2% (round to 0dp) or -2%
 * and ±0% if 0%"): the ONE tempo-offset formatter, shared by the jog dial and
 * the deck header so the two readouts cannot drift. Pure, so a unit test can
 * load it without a component. */

/** Tempo offset of a pitch ratio (1 = original tempo), rounded to whole
 * percent: "+2%", "-2%", and "±0%" for anything that rounds to zero (so -0.4%
 * reads "±0%", never "-0%"). */
export function formatTempoPct(pitch: number): string {
	const pct = Math.round((pitch - 1) * 100);
	// Math.round(-0.4) is -0; compare by value so it lands here too.
	if (pct === 0) return '±0%';
	return pct > 0 ? `+${pct}%` : `${pct}%`;
}

/** The pitch range readout: "±16", or "WIDE" for the 100% range. */
export function formatPitchRange(rangePct: number): string {
	return rangePct === 100 ? 'WIDE' : `±${rangePct}`;
}

/** Splits a BPM readout into the whole part and the "." plus decimals, so the
 * decimals can be drawn smaller. "--.--" (no BPM) splits the same way. */
export function splitBpmText(text: string): { whole: string; fraction: string } {
	const dot = text.indexOf('.');
	return dot < 0
		? { whole: text, fraction: '' }
		: { whole: text.slice(0, dot), fraction: text.slice(dot) };
}
