import { nativeShellKind, openExternal } from '$lib/shell/native-shell';
import { hostedBilling, LIFECYCLE_STATES, type FeatureStanding } from './hosted-billing.svelte';

// Armed only once a checkout or portal page opened; one focus refresh disarms it.
let refreshOnFocus = false;

type Json = Record<string, unknown>;

function fail(what: string): never {
	throw new Error(what);
}

// Strict on purpose: a drifted or malformed answer is an error, never a
// default that reads as "not eligible" or "no subscription".
async function billingCall(path: string, init?: RequestInit): Promise<Json> {
	const response = await fetch(`/api/v1/sync/billing/${path}`, init);
	const body: unknown = await response.json().catch(() => null);
	if (body === null || typeof body !== 'object' || Array.isArray(body)) {
		fail(`${path}: HTTP ${response.status}, no JSON object`);
	}
	if (!response.ok) {
		const message = ((body as Json).detail as { message?: unknown } | undefined)?.message;
		fail(typeof message === 'string' ? message : `${path}: HTTP ${response.status}`);
	}
	return body as Json;
}

function isNullableString(value: unknown): value is string | null {
	return value === null || typeof value === 'string';
}

// A state outside the hub's closed set is drift, never a plan status to show.
function isLifecycleState(value: unknown): value is FeatureStanding['state'] {
	return value === null || (LIFECYCLE_STATES as readonly unknown[]).includes(value);
}

// The same exact-Polar rule the spoke's eligibility route applies: billing
// opens only against Polar, so any other provider in an answer is drift.
export function isPolarProvider(value: unknown): value is string {
	return typeof value === 'string' && (value === 'polar' || value.startsWith('polar:'));
}

async function eligibility(): Promise<{
	eligible: boolean;
	provider: string | null;
}> {
	const body = await billingCall('eligibility');
	if (typeof body.eligible !== 'boolean' || !isNullableString(body.entitlement_provider)) {
		fail('bad eligibility answer');
	}
	return { eligible: body.eligible, provider: body.entitlement_provider };
}

function postBilling(path: 'checkout' | 'portal' | 'refresh', body: Json): Promise<Json> {
	return billingCall(path, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
}

export async function loadHostedBilling(): Promise<void> {
	refreshOnFocus = false;
	hostedBilling.loading = true;
	hostedBilling.error = null;
	hostedBilling.eligible = false;
	try {
		const gate = await eligibility();
		hostedBilling.eligible = gate.eligible;
		hostedBilling.provider = gate.provider;
		if (!gate.eligible) {
			hostedBilling.standings = [];
			return;
		}
		await refreshHostedBilling();
	} catch (exc) {
		// Keep `eligible` from a gate that answered: a failed refresh on an
		// eligible hub must stay visible as an error, not hide the section.
		hostedBilling.standings = [];
		hostedBilling.error = exc instanceof Error ? exc.message : String(exc);
	} finally {
		hostedBilling.loading = false;
	}
}

export async function refreshHostedBilling(): Promise<void> {
	if (!hostedBilling.eligible) return;
	const payload = await postBilling('refresh', {});
	const rows = payload.standings;
	if (
		!isPolarProvider(payload.provider) ||
		!Array.isArray(rows) ||
		!rows.every(
			(row: Json) => typeof row?.feature_id === 'string' && isLifecycleState(row.state)
		)
	) {
		fail('bad refresh answer');
	}
	hostedBilling.standings = rows as FeatureStanding[];
	hostedBilling.provider = payload.provider;
	hostedBilling.error = null;
}

// Only an https page on Polar's own domain is opened: the url came over the
// network, and a javascript: or foreign url would run with this page's rights.
function polarUrl(path: string, raw: string): string {
	let parsed: URL;
	try {
		parsed = new URL(raw);
	} catch {
		fail(`${path}: unsafe url`);
	}
	const host = parsed.hostname;
	if (parsed.protocol !== 'https:' || !(host === 'polar.sh' || host.endsWith('.polar.sh'))) {
		fail(`${path}: unsafe url`);
	}
	return parsed.href;
}

async function openPolarUrl(
	path: 'checkout' | 'portal',
	body: Record<string, unknown>
): Promise<void> {
	if (!hostedBilling.eligible) return;
	// A desktop shell opens the system browser. A browser tab must open its
	// new tab inside the click, before any await, or the popup blocker eats it.
	const inShell = nativeShellKind() !== null;
	const tab = inShell ? null : window.open('about:blank', '_blank');
	if (tab !== null) tab.opener = null;
	try {
		const payload = await postBilling(path, body);
		if (typeof payload.url !== 'string' || !payload.url) {
			fail(`${path}: no url`);
		}
		const url = polarUrl(path, payload.url);
		if (inShell) await openExternal(url);
		else if (tab !== null) tab.location.href = url;
		else fail('popup blocked: allow pop-ups to open Polar');
		refreshOnFocus = true;
	} catch (exc) {
		tab?.close();
		throw exc;
	}
}

export async function openHostedCheckout(): Promise<void> {
	await openPolarUrl('checkout', { feature_id: 'cloudsync.hosted_hub' });
}

export async function openHostedPortal(): Promise<void> {
	await openPolarUrl('portal', {});
}

export async function onHostedBillingWindowFocus(): Promise<void> {
	if (!refreshOnFocus || !hostedBilling.eligible) return;
	refreshOnFocus = false;
	try {
		await refreshHostedBilling();
	} catch (exc) {
		hostedBilling.error = exc instanceof Error ? exc.message : String(exc);
	}
}
