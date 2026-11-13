/**
 * Query-string rules for the boot and onboarding redirects: the incoming query
 * survives the redirect, and `tempo_*` keys are validated on the way through.
 *
 * A leaf module on purpose. `rb/boot-landing.ts` imports it, and the browser
 * tests' support code loads `rb/boot-landing.ts` outside Vite, so nothing here
 * may reach the API client, which reads `import.meta.env` when it loads.
 * `wizard-rules.ts` re-exports every name below for its own callers.
 */
import { tempoPrefFieldValue, tempoPrefRangeInvalid } from '../rb/tempo-pref-fields';

export const ONBOARDING_TEMPO_SEARCH_KEYS = ['tempo_regular', 'tempo_min', 'tempo_max'] as const;

function tempoValueFromQueryRaw(raw: string | null): number | null {
	if (raw === null || raw === '') return null;
	return tempoPrefFieldValue(Number(raw));
}

/** Preserve incoming query on onboarding / boot redirects; `tempo_*` keys are validated. */
export function preserveUrlSearchOnRedirect(pathname: string, search: string): string {
	const trimmed = search.trim();
	if (trimmed === '') return pathname;

	const raw = trimmed.startsWith('?') ? trimmed.slice(1) : trimmed;
	const params = new URLSearchParams(raw);
	const tempoKeySet = new Set<string>(ONBOARDING_TEMPO_SEARCH_KEYS);
	const validTempo = new URLSearchParams(filterValidTempoSearchParams(search).replace(/^\?/, ''));
	const merged = new URLSearchParams();
	for (const [key, value] of params) {
		if (tempoKeySet.has(key)) {
			const validated = validTempo.get(key);
			if (validated !== null) merged.append(key, validated);
		} else {
			merged.append(key, value);
		}
	}

	const encoded = merged.toString();
	return encoded === '' ? pathname : `${pathname}?${encoded}`;
}

/** Validated tempo query keys only; invalid keys and bad min/max pairs are omitted. */
export function filterValidTempoSearchParams(search: string): string {
	const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
	const regular = tempoValueFromQueryRaw(params.get('tempo_regular'));
	let min = tempoValueFromQueryRaw(params.get('tempo_min'));
	let max = tempoValueFromQueryRaw(params.get('tempo_max'));
	if (tempoPrefRangeInvalid(min, max)) {
		min = null;
		max = null;
	}
	const out = new URLSearchParams();
	if (regular !== null) out.set('tempo_regular', String(regular));
	if (min !== null) out.set('tempo_min', String(min));
	if (max !== null) out.set('tempo_max', String(max));
	const encoded = out.toString();
	return encoded === '' ? '' : `?${encoded}`;
}

/** Host route plus validated tempo query (setup handoff acceptance surface). */
export function appendTempoSearchToRoute(route: string, search: string): string {
	const suffix = filterValidTempoSearchParams(search);
	return suffix === '' ? route : `${route}${suffix}`;
}
