/**
 * Framework-neutral split-core solver for one resizable group (one
 * direction, flat children). Infeasible input throws; nothing is clamped.
 */

export interface SplitChild {
	id: string;
	min?: number;
	max?: number;
	default?: number;
	flex?: boolean;
	collapsible?: boolean;
	collapsedSize?: number;
}

export interface SplitGroup {
	id: string;
	children: SplitChild[];
}

export interface UserSizes {
	sizes?: Record<string, number>;
	collapsed?: Record<string, boolean>;
}

export function solveLayout(
	container: number,
	group: SplitGroup,
	user: UserSizes = {}
): Record<string, number> {
	const flex = group.children.filter((c) => c.flex);
	if (flex.length !== 1) {
		throw new RangeError(
			`group "${group.id}" must have exactly one flex child, found ${flex.length}`
		);
	}
	const minSum = group.children.reduce((s, c) => s + (c.min ?? 0), 0);
	if (minSum > container) {
		throw new RangeError(
			`container size ${container} is smaller than summed minimum ${minSum} in group "${group.id}"`
		);
	}
	const out: Record<string, number> = {};
	let used = 0;
	for (const c of group.children) {
		if (c.flex) continue;
		const min = c.min ?? 0;
		const max = c.max ?? Infinity;
		let size: number;
		if (c.collapsible && user.collapsed?.[c.id]) {
			size = c.collapsedSize ?? 0;
		} else {
			size = user.sizes?.[c.id] ?? c.default ?? min;
			if (size < min || size > max) {
				throw new RangeError(
					`child "${c.id}" size ${size} is outside allowed range ${min}..${max}`
				);
			}
		}
		out[c.id] = size;
		used += size;
	}
	const f = flex[0];
	const fMin = f.min ?? 0;
	const fMax = f.max ?? Infinity;
	const rest = container - used;
	if (rest < fMin || rest > fMax) {
		throw new RangeError(
			`flex child "${f.id}" remainder ${rest} is outside allowed range ${fMin}..${fMax}`
		);
	}
	out[f.id] = rest;
	return out;
}
