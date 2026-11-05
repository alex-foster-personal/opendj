/**
 * How long comment pins stay on screen, and which ones still count as new.
 *
 * Pin 8bd1522ce260: a day count hides older pins. 0 shows every pin.
 * A pin is new when it is under 3 days old.
 */

export const PIN_VISIBLE_DAYS_MIN = 0;
export const PIN_VISIBLE_DAYS_MAX = 365;
/** 0 shows every pin until the maintainer picks a window. */
export const PIN_VISIBLE_DAYS_DEFAULT = 0;
export const PIN_NEW_MAX_DAYS = 3;

const MS_PER_DAY = 24 * 60 * 60 * 1000;

export function isPinVisibleDays(value: unknown): value is number {
	return (
		typeof value === 'number' &&
		Number.isInteger(value) &&
		value >= PIN_VISIBLE_DAYS_MIN &&
		value <= PIN_VISIBLE_DAYS_MAX
	);
}

/** Age in days, or null when created_at is not a timestamp. */
export function pinAgeDays(createdAt: string, nowMs: number): number | null {
	const created = Date.parse(createdAt);
	if (Number.isNaN(created)) return null;
	return (nowMs - created) / MS_PER_DAY;
}

/** 0 (or any non-positive count) keeps every pin. Older than the count is hidden. */
export function isPinWithinVisibleDays(
	createdAt: string,
	visibleDays: number,
	nowMs: number
): boolean {
	if (!(visibleDays > 0)) return true;
	const age = pinAgeDays(createdAt, nowMs);
	if (age === null) return true;
	return age <= visibleDays;
}

/** New means under 3 days old. A future timestamp still counts as new. */
export function isNewPin(createdAt: string, nowMs: number): boolean {
	const age = pinAgeDays(createdAt, nowMs);
	return age !== null && age < PIN_NEW_MAX_DAYS;
}
