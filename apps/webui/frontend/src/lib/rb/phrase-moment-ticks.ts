/**
 * STRUCT-07: drop and break moments as deck-strip ticks.
 *
 * Pure: the strip renders what this returns. Only numbered (real) drops and
 * breaks get a tick; fake drops and build moments do not, so the strip shows
 * "D1", "D2" and "B" as in the drop-label proposal. Each tick carries a hover
 * title saying what the moment is.
 *
 * -Claude
 */

import type { AnlzPhraseMoment } from './anlz-types';

export interface PhraseMomentTick {
	kind: 'drop' | 'break';
	leftPct: number;
	text: string;
	title: string;
}

function _fmtMmSs(s: number): string {
	const totalS = Math.max(0, Math.floor(s));
	const m = Math.floor(totalS / 60);
	return `${String(m).padStart(2, '0')}:${String(totalS % 60).padStart(2, '0')}`;
}

export function phraseMomentTicks(
	moments: readonly AnlzPhraseMoment[] | undefined,
	durationMs: number | null
): PhraseMomentTick[] {
	if (moments === undefined || durationMs === null || durationMs <= 0) return [];
	return moments.flatMap((m): PhraseMomentTick[] => {
		if (m.kind === 'drop' && (m.fake === true || m.number == null)) return [];
		if (m.kind !== 'drop' && m.kind !== 'break') return [];
		const at = _fmtMmSs(m.time_s);
		const bar = m.bar !== null ? ` (bar ${m.bar + 1})` : '';
		const leftPct = ((m.time_s * 1000) / durationMs) * 100;
		if (m.kind === 'drop') {
			return [
				{
					kind: 'drop',
					leftPct,
					text: `D${m.number}`,
					title: `Drop ${m.number} at ${at}${bar}: the first downbeat where drums and bass come back after a break or build`
				}
			];
		}
		return [
			{
				kind: 'break',
				leftPct,
				text: 'B',
				title: `Break at ${at}${bar}: drums or bass pull out after a drop`
			}
		];
	});
}
