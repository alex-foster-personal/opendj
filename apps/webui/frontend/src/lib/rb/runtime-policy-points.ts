/** Mutable ANLZ points default shared by runtime-policy hydration and api-rb. */

let _anlzPointsDefault = 38400;

export function setAnlzPointsDefault(points: number): void {
	if (!Number.isFinite(points) || points <= 0) {
		throw new Error(`runtime policy: anlz_points_default must be a positive number, got ${points}`);
	}
	_anlzPointsDefault = points;
}

export function defaultAnlzPoints(): number {
	return _anlzPointsDefault;
}
