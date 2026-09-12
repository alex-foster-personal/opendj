/**
 * Read-only snapshot of capability, entitlement, and build-flag stores for
 * the /admin entitlements inspector (ADMIN-01).
 *
 * Calls the existing *Refusal() helpers; never re-derives refusal sentences.
 */

import {
	capabilities,
	jobsRefusal,
	eventsRefusal,
	progressRefusal
} from '$lib/api/capabilities.svelte';
import { entitlements, planRefusal } from '$lib/api/entitlements.svelte';
import { buildFlags, storeBuildRefusal } from '$lib/api/store-build.svelte';
import { rekordboxWritebackRefusal } from '$lib/rb/rekordbox-writeback.svelte';
import { finalSetupRefusal } from '$lib/setup/setup-api';

export type InspectorKind = 'capability' | 'entitlement' | 'flag' | 'gate' | 'inert';

export type InspectorRow = {
	id: string;
	kind: InspectorKind;
	label: string;
	value: string;
	refusal: string | null;
};

export type InspectorSnapshot = {
	daemonFlavor: string;
	planLabel: string | null;
	buildProfile: string;
	sandboxed: boolean;
	entitlementsLoaded: boolean;
	entitlementsError: string | null;
	flagsLoaded: boolean;
	flagsError: string | null;
	capabilitiesError: string | null;
	rows: InspectorRow[];
	activeRefusals: InspectorRow[];
};

const INERT_PARITY_TODO = 'not implemented - see PARITY-TODO';

function boolValue(on: boolean): string {
	return on ? 'true' : 'false';
}

function planValue(): string {
	if (!entitlements.loaded) return 'not loaded';
	return entitlements.plan?.label ?? 'none';
}

function featureValue(entitled: boolean, quota: number | null | undefined): string {
	const base = entitled ? 'entitled' : 'not entitled';
	if (quota !== null && quota !== undefined) return `${base} (quota ${quota})`;
	return base;
}

export function collectInspectorSnapshot(): InspectorSnapshot {
	const rows: InspectorRow[] = [];

	rows.push({
		id: 'daemon.flavor',
		kind: 'capability',
		label: 'Daemon flavor',
		value: capabilities.flavor,
		refusal: null
	});
	rows.push({
		id: 'jobs',
		kind: 'capability',
		label: 'jobs',
		value: boolValue(capabilities.jobs),
		refusal: jobsRefusal()
	});
	rows.push({
		id: 'events',
		kind: 'capability',
		label: 'events',
		value: boolValue(capabilities.events),
		refusal: eventsRefusal()
	});
	rows.push({
		id: 'progressLedger',
		kind: 'capability',
		label: 'progressLedger',
		value: boolValue(capabilities.progressLedger),
		refusal: progressRefusal()
	});

	rows.push({
		id: 'plan',
		kind: 'entitlement',
		label: 'Plan',
		value: planValue(),
		refusal: null
	});
	for (const feature of entitlements.features) {
		rows.push({
			id: `plan.${feature.feature_id}`,
			kind: 'entitlement',
			label: feature.feature_id,
			value: featureValue(feature.entitled, feature.quota),
			refusal: planRefusal(feature.feature_id)
		});
	}

	if (buildFlags.loaded) {
		rows.push({
			id: 'build.profile',
			kind: 'flag',
			label: 'Build profile',
			value: buildFlags.profile,
			refusal: null
		});
		rows.push({
			id: 'build.sandboxed',
			kind: 'flag',
			label: 'Sandboxed',
			value: boolValue(buildFlags.sandboxed),
			refusal: null
		});
		for (const flag of buildFlags.flags) {
			rows.push({
				id: flag.flag_id,
				kind: 'flag',
				label: flag.flag_id,
				value: flag.enabled ? 'on' : 'off',
				refusal: storeBuildRefusal(flag.flag_id)
			});
		}
	}

	rows.push({
		id: 'setup',
		kind: 'gate',
		label: 'Setup',
		value: finalSetupRefusal() === null ? 'offered' : 'refused',
		refusal: finalSetupRefusal()
	});
	rows.push({
		id: 'rekordbox.writeback',
		kind: 'gate',
		label: 'Rekordbox writeback',
		value: rekordboxWritebackRefusal() === null ? 'enabled' : 'disabled',
		refusal: rekordboxWritebackRefusal()
	});

	rows.push({
		id: 'inert.parity-todo',
		kind: 'inert',
		label: 'PARITY-TODO inert controls',
		value: 'not built',
		refusal: INERT_PARITY_TODO
	});

	const activeRefusals = rows.filter(
		(row) => row.refusal !== null && row.id !== 'inert.parity-todo'
	);

	return {
		daemonFlavor: capabilities.flavor,
		planLabel: entitlements.loaded ? entitlements.plan?.label ?? null : null,
		buildProfile: buildFlags.profile,
		sandboxed: buildFlags.sandboxed,
		entitlementsLoaded: entitlements.loaded,
		entitlementsError: entitlements.error,
		flagsLoaded: buildFlags.loaded,
		flagsError: buildFlags.error,
		capabilitiesError: capabilities.error,
		rows,
		activeRefusals
	};
}
