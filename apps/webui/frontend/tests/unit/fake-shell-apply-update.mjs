let applyCalls = 0;

export async function applyUpdate() {
	applyCalls += 1;
	globalThis.__shellApplyCalls = applyCalls;
	return { kind: 'installed' };
}

export function canApplyHere() {
	return true;
}
