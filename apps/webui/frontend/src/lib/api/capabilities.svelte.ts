/**
 * WHICH DAEMON IS SERVING THIS SPA, decided once, from one request.
 *
 * The same build of this frontend is served by two different daemons and they
 * do not offer the same API:
 *
 *   legacy (apps/webui/server/app.py) -- serves /api/v1/progress (the fan-out
 *     ledger). Has NO jobs API and NO /api/v1/events socket.
 *   engine (apps/engine_core/app.py) -- serves the same progress router, re-owns
 *     /api/v1/health, and ADDS /api/v1/jobs* plus the /api/v1/events WebSocket
 *     hub.
 *
 * Without this module each surface finds that out the expensive way: the jobs
 * drawer 404s on a legacy boot, the events bus reconnects forever against a
 * route that does not exist, and /progress-tree refetches a 404 every 30
 * seconds against the engine. That is one probe's worth of information paid
 * for over and over, per component, forever.
 *
 * THE PROBE. One GET /api/v1/health, which BOTH daemons serve, so the probe
 * itself can never be the 404 it is meant to prevent. The engine's response is
 * the legacy body plus contract_rev / engine_version / boot_id (see
 * EngineHealthOut in apps/engine_core/app.py), so the presence of those three
 * fields IS the discriminator. It is checked on the bytes, not on the
 * generated type: apps/webui/openapi.json is the ENGINE contract, so
 * api-types.ts types this route as EngineHealthOut unconditionally -- a claim
 * about the schema, never about what the daemon actually answered.
 *
 * NO POLLING, NO GUESSING. The probe is memoized once it succeeds. A FAILED
 * probe is not memoized (the daemon may simply not be up yet), so the next
 * caller retries; that is demand-driven, never a loop. Until a probe succeeds
 * the flavor is 'unknown' and EVERY daemon-specific surface stays inert. An
 * unknown daemon is never treated as either one, because guessing wrong is
 * exactly the 404 storm this module exists to stop.
 *
 * DIAGNOSTICS FIELDS. `probedAt` records when the last probe attempt settled
 * (success or failure). `handshake` carries the engine's contract_rev /
 * engine_version / boot_id when flavor is 'engine', else null.
 *
 * Requirements (mini-PRD):
 *   ✔︎ 🎯 readDaemonFlavor(body): engine iff all three handshake fields are
 *     present, legacy iff none are, explicit throw for a partial set.
 *     [if] a body carrying only engine_version is called 'engine' [then ⛔️] broken
 *   ✔︎ 🎯 probe(): one health GET, memoized on success, retried after failure.
 *     [if] two probe() calls issue two requests after a success [then ⛔️] broken
 *   ✔︎ 🎯 a failed or unresolved probe leaves jobs/events/progressLedger all
 *     false, so no surface fires a request it cannot know is served.
 *     [if] progressLedger reads true while flavor is 'unknown' [then ⛔️] broken
 */

import { api, unwrap } from './client';

export type DaemonFlavor = 'engine' | 'legacy' | 'unknown';

/** The engine's health additions. All three, or none: a partial set means the
 * daemon changed shape and this module's discriminator is no longer sound. */
const ENGINE_HEALTH_FIELDS = ['contract_rev', 'engine_version', 'boot_id'] as const;

/** What each surface is told when the daemon does not offer it. Deliberately
 * NOT the PARITY-TODO stub wording that inert-controls.test.mjs polices: that
 * one means "not built". These features are built and work; they are simply
 * not served by the daemon behind this page, which is a different fact and
 * deserves a different sentence. */
const JOBS_MISSING = 'jobs API not offered by this daemon (no /api/v1/jobs on a legacy boot)';
const EVENTS_MISSING = 'event bus not offered by this daemon (no /api/v1/events on a legacy boot)';
const UNIDENTIFIED = 'daemon not identified yet: GET /api/v1/health has not answered';

function _message(exc: unknown): string {
	return exc instanceof Error ? exc.message : String(exc);
}

/**
 * Read the daemon flavor off a /api/v1/health body.
 *
 * Throws rather than returning 'unknown' for a body this module cannot read:
 * a non-object response or a HALF-present handshake is a contract break
 * somebody must fix, and swallowing it would turn a broken daemon into a
 * silently degraded UI.
 */
export function readDaemonFlavor(body: unknown): DaemonFlavor {
	if (typeof body !== 'object' || body === null || Array.isArray(body)) {
		const shape = Array.isArray(body) ? 'an array' : `a ${body === null ? 'null' : typeof body}`;
		throw new Error(`capabilities: /api/v1/health answered ${shape}, expected a JSON object`);
	}
	const health = body as Record<string, unknown>;
	const present = ENGINE_HEALTH_FIELDS.filter(
		(field) => typeof health[field] === 'string' && health[field] !== ''
	);
	if (present.length === ENGINE_HEALTH_FIELDS.length) return 'engine';
	if (present.length === 0) return 'legacy';
	throw new Error(
		`capabilities: /api/v1/health carries ${present.join(', ')} but not ` +
			`${ENGINE_HEALTH_FIELDS.filter((field) => !present.includes(field)).join(', ')}; ` +
			'the engine health contract changed shape'
	);
}

class CapabilityStore {
	/** 'unknown' until a probe succeeds. Never inferred from anything else. */
	flavor = $state<DaemonFlavor>('unknown');
	/** Last probe failure, verbatim. null while healthy or before the first try. */
	error = $state<string | null>(null);
	/** ISO timestamp when the last probe attempt settled, or null before any try. */
	probedAt = $state<string | null>(null);
	/** Engine handshake fields from the probe body, or null for legacy/unknown. */
	handshake = $state<{
		contract_rev: string;
		engine_version: string;
		boot_id: string;
	} | null>(null);
	/** `google_oauth_configured` as the daemon stated it. null is UNKNOWN (no
	 * answer yet, or a daemon that predates the field); false is a daemon that
	 * HAS answered and has no OAuth client. Both health flavors carry it. */
	googleOAuthConfigured = $state<boolean | null>(null);

	/** In flight or settled-successful probe. Cleared on failure so the next
	 * caller retries instead of inheriting a verdict of "we never found out". */
	#probe: Promise<DaemonFlavor> | null = null;

	/** GET /api/v1/jobs and friends. Engine only. */
	get jobs(): boolean {
		return this.flavor === 'engine';
	}

	/** The /api/v1/events WebSocket invalidation bus. Engine only. */
	get events(): boolean {
		return this.flavor === 'engine';
	}

	/** GET /api/v1/progress, the fan-out ledger. Engine and legacy both serve it. */
	get progressLedger(): boolean {
		return this.flavor === 'engine' || this.flavor === 'legacy';
	}

	/**
	 * Resolve the flavor. Safe to call from every surface that needs it: the
	 * first success is memoized and every later call is free.
	 */
	async probe(): Promise<DaemonFlavor> {
		if (this.#probe !== null) return this.#probe;
		const running = this.#run();
		this.#probe = running;
		return running;
	}

	async #run(): Promise<DaemonFlavor> {
		try {
			const body = await unwrap(api.GET('/api/v1/health'));
			this.flavor = readDaemonFlavor(body);
			this.googleOAuthConfigured = readGoogleOAuthConfigured(body);
			this.error = null;
			if (this.flavor === 'engine') {
				const health = body as Record<string, unknown>;
				this.handshake = {
					contract_rev: String(health.contract_rev),
					engine_version: String(health.engine_version),
					boot_id: String(health.boot_id)
				};
			} else {
				this.handshake = null;
			}
		} catch (exc) {
			// Not memoized: a daemon that was down at page load may be up by
			// the time the next surface asks.
			this.#probe = null;
			this.flavor = 'unknown';
			this.handshake = null;
			this.googleOAuthConfigured = null;
			this.error = _message(exc);
			console.error('[capabilities] probe failed; daemon-specific surfaces stay inert', exc);
		}
		this.probedAt = new Date().toISOString();
		return this.flavor;
	}

	/** Drop the memo, for tests. App code probes once and lives with it. */
	_resetForTests(): void {
		this.#probe = null;
		this.flavor = 'unknown';
		this.error = null;
		this.probedAt = null;
		this.handshake = null;
		this.googleOAuthConfigured = null;
	}
}

/**
 * Read the daemon's Google OAuth answer off a /api/v1/health body.
 *
 * THREE states, not two. `false` is the daemon saying it has no OAuth client,
 * which is what makes a sign-in control read as unavailable rather than fail
 * after the click. `true` is a configured client. `null` is UNKNOWN: nothing
 * has answered yet, or the daemon predates the field. Collapsing unknown into
 * false would disable sign-in on every boot frame and on every older daemon,
 * which is a different (and worse) lie than the one this fixes.
 *
 * A present-but-non-boolean value is a contract break and throws, matching
 * readDaemonFlavor above: a half-read field must not pass as an answer.
 */
export function readGoogleOAuthConfigured(body: object): boolean | null {
	const raw = (body as Record<string, unknown>).google_oauth_configured;
	if (raw === undefined) return null;
	if (typeof raw !== 'boolean') {
		throw new Error(
			`capabilities: /api/v1/health answered google_oauth_configured as ` +
				`${typeof raw}, expected a boolean`
		);
	}
	return raw;
}

/** The one capability store. */
export const capabilities = new CapabilityStore();

/**
 * Why the jobs surface is inert, or null when the daemon offers it.
 *
 * Mirrors the cancelRefusal/reenqueueRefusal shape in jobs-store: one function
 * that both gates the request and supplies the tooltip, so a disabled control
 * can never disagree with the reason it is disabled.
 */
export function jobsRefusal(): string | null {
	if (capabilities.flavor === 'engine') return null;
	return capabilities.flavor === 'legacy' ? JOBS_MISSING : UNIDENTIFIED;
}

/** Why the progress ledger surface is inert, or null when it is offered. */
export function progressRefusal(): string | null {
	if (capabilities.flavor === 'engine' || capabilities.flavor === 'legacy') return null;
	return UNIDENTIFIED;
}

/** Why the events bus is not connected, or null when it is offered. */
export function eventsRefusal(): string | null {
	if (capabilities.flavor === 'engine') return null;
	return capabilities.flavor === 'legacy' ? EVENTS_MISSING : UNIDENTIFIED;
}
