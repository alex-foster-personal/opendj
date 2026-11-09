/**
 * Taste variants behind developer-only settings (epic #10045).
 *
 * A taste-carrying constant ships as 2 to 4 named variants behind a dev-only
 * enum row so the maintainer flips between them while he plays; the in-play pick log or
 * a blind A/B session sets the default, then a one-line constants PR retires the
 * row. Hygiene values stay mimicked, never variants.
 *
 * A family registers by being imported: the owner imports its def and calls
 * `readVariant(DEF.id)`; `catalog.ts` imports the family module so the row
 * shows. This module must not import prefs (prefs imports it).
 */
import type { SettingDef, SettingGroupId } from './catalog-types';

export interface VariantOption {
	value: string;
	label: string;
	summary: string;
}

export interface VariantSettingSpec {
	id: string;
	label: string;
	group: SettingGroupId;
	keywords: readonly string[];
	title?: string;
	detail: string;
	variants: readonly VariantOption[];
	defaultValue: string;
}

export type VariantSettingDef = SettingDef & {
	devOnly: true;
	implemented: true;
	control: { kind: 'enum'; options: ReadonlyArray<{ value: string; label: string }> };
	variants: readonly VariantOption[];
	defaultValue: string;
};

export type VariantSettingId = `variant_${string}`;

export const VARIANT_MIN = 2;
export const VARIANT_MAX = 4;

const VARIANT_ID_RE = /^variant_[a-z0-9]+_[a-z0-9_]+$/;

const _byId = new Map<string, VariantSettingDef>();

/** Every defined variant setting, in definition order. Empty until a family registers. */
export const VARIANT_REGISTRY: VariantSettingDef[] = [];

export function defineVariantSetting(spec: VariantSettingSpec): VariantSettingDef {
	const prefix = `variant setting ${spec.id}:`;

	if (!VARIANT_ID_RE.test(spec.id)) {
		throw new Error(`${prefix} id must look like variant_<family>_<name>`);
	}
	if (_byId.has(spec.id)) {
		throw new Error(`${prefix} already defined`);
	}
	if (spec.variants.length < VARIANT_MIN || spec.variants.length > VARIANT_MAX) {
		throw new Error(
			`${prefix} must have ${VARIANT_MIN}..${VARIANT_MAX} variants, got ${spec.variants.length}`
		);
	}

	const seenValues = new Set<string>();
	for (const v of spec.variants) {
		if (!v.value || !v.label || !v.summary) {
			throw new Error(`${prefix} each variant needs a non-empty value, label and summary`);
		}
		if (seenValues.has(v.value)) {
			throw new Error(`${prefix} duplicate variant value ${JSON.stringify(v.value)}`);
		}
		seenValues.add(v.value);
	}

	if (!seenValues.has(spec.defaultValue)) {
		const allowed = spec.variants.map((v) => v.value);
		throw new Error(
			`${prefix} defaultValue ${JSON.stringify(spec.defaultValue)} is not one of ` +
				`${allowed.join('|')}`
		);
	}

	const defaultLabel =
		spec.variants.find((v) => v.value === spec.defaultValue)?.label ?? spec.defaultValue;
	const variantDetail = spec.variants.map((v) => `${v.label}: ${v.summary}`).join('; ');
	const detail =
		`${spec.detail} ${variantDetail}. Default: ${defaultLabel}. ` +
		`Developer-only, saved in this browser only.`;

	const options = Object.freeze(
		spec.variants.map(({ value, label }) => Object.freeze({ value, label }))
	);
	const variants = Object.freeze(spec.variants.map((v) => Object.freeze({ ...v })));

	const def: VariantSettingDef = Object.freeze({
		id: spec.id,
		label: spec.label,
		group: spec.group,
		keywords: Object.freeze([...spec.keywords]),
		title: spec.title ?? spec.label,
		detail,
		implemented: true,
		devOnly: true,
		control: Object.freeze({ kind: 'enum' as const, options }),
		variants,
		defaultValue: spec.defaultValue
	});

	_byId.set(spec.id, def);
	VARIANT_REGISTRY.push(def);
	return def;
}

export function isVariantSettingId(id: string): boolean {
	return _byId.has(id);
}

export function variantSetting(id: string): VariantSettingDef {
	const def = _byId.get(id);
	if (!def) {
		const registered = [..._byId.keys()].join(', ') || '(none)';
		throw new Error(`unknown variant setting ${id} (registered: ${registered})`);
	}
	return def;
}

export function checkVariantValue(id: string, value: unknown, where: string): string {
	const def = variantSetting(id);
	const values = def.control.options.map((o) => o.value);
	if (typeof value !== 'string' || !values.includes(value)) {
		throw new Error(
			`${where}: variant ${id} value ${JSON.stringify(value)} is not one of ` +
				`${values.join('|')} - clear the localStorage key to recover`
		);
	}
	return value;
}
