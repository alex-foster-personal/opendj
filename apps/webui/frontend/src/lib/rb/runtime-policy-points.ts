/**
 * ANLZ points default shared by runtime-policy hydration and api-rb.
 *
 * null until GET /api/v1/settings has answered. Until then /anlz requests
 * carry no `points` and the server applies its own configured default, so a
 * deck restore that races hydration can never ask for a value an operator's
 * MDT_ANLZ_POINTS_MAX forbids (#3739 P1: a hard-coded 38400 here 422'd every
 * early load on a box configured for 19200).
 */

let _anlzPointsDefault: number | null = null;

export function setAnlzPointsDefault(points: number): void {
	if (!Number.isFinite(points) || points <= 0) {
		throw new Error(`runtime policy: anlz_points_default must be a positive number, got ${points}`);
	}
	_anlzPointsDefault = points;
}

/** The hydrated default, or null while the server's own default must apply. */
export function defaultAnlzPoints(): number | null {
	return _anlzPointsDefault;
}

/** Query string for GET /tracks/{sid}/anlz: `points` only when it is known. */
export function anlzQuery(points: number | null, gen: number | string): string {
	return points === null ? `gen=${gen}` : `points=${points}&gen=${gen}`;
}
