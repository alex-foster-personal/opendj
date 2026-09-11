/** In-memory optional-resource caps remembered from TrackOut responses.
 * Lets the browser skip GETs that would 404 (Chromium logs those as
 * console.error). Unknown means "fetch as today". */

export type OptionalResourceCaps = {
	lyrics: boolean | 'unknown';
	autoCues: boolean | 'unknown';
	stems: boolean | 'unknown';
};

type StoreGlobal = {
	__musicDjToolsOptionalResourceStore?: Map<string, Partial<OptionalResourceCaps>>;
};

function _store(): Map<string, Partial<OptionalResourceCaps>> {
	const g = globalThis as StoreGlobal;
	if (!g.__musicDjToolsOptionalResourceStore) {
		g.__musicDjToolsOptionalResourceStore = new Map();
	}
	return g.__musicDjToolsOptionalResourceStore;
}

export function rememberOptionalResources(
	stableId: string,
	partial: {
		lyrics?: boolean;
		autoCues?: boolean;
		stems?: boolean;
	}
): void {
	const prev = _store().get(stableId) ?? {};
	const next: Partial<OptionalResourceCaps> = { ...prev };
	if (typeof partial.lyrics === 'boolean') {
		next.lyrics = partial.lyrics;
	}
	if (typeof partial.autoCues === 'boolean') {
		next.autoCues = partial.autoCues;
	}
	if (typeof partial.stems === 'boolean') {
		next.stems = partial.stems;
	}
	_store().set(stableId, next);
}

export function optionalResources(stableId: string): OptionalResourceCaps {
	const caps = _store().get(stableId);
	return {
		lyrics: caps?.lyrics ?? 'unknown',
		autoCues: caps?.autoCues ?? 'unknown',
		stems: caps?.stems ?? 'unknown'
	};
}

/** Unit tests only. */
export function resetOptionalResourcesForTests(): void {
	_store().clear();
}
