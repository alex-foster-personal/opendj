/**
 * Liveness-light policy for the Frontend / Backend dots and the coverage
 * dots' timeout case (HEALTH-8304, HEALTH-8304b, HEALTH-8304c). Pure, so the state
 * transitions are unit tested without a component or a network.
 *
 * The rules:
 *
 * - A probe that answers is green, and resets the failure count.
 * - A probe that TIMES OUT while the engine has not answered once since the
 *   page loaded, inside the boot window, is amber "starting up": the engine
 *   serves this page, so it is up, and a slow answer at boot is it booting
 *   (cache warm-up, the analysis worker), not it being down.
 * - Any other failure is amber "retrying" until it has failed
 *   `FAILURES_BEFORE_DOWN` times in a row; only then is it red. One missed
 *   ping is never a verdict.
 * - In a packaged app the page and the API come from the same engine, so
 *   there is no dev server to probe: the row reads "App UI - served by the
 *   engine" and mirrors the Backend dot, with no second request.
 * - A coverage read that times out is amber "still counting", never grey
 *   "unknown": the engine is busy measuring, and the re-ask schedule asks
 *   again. A verdict already painted is kept, marked as being re-checked.
 */

import type { LibraryHealthDot } from "./library-health-dots";

/** How long after page load a timeout reads as "starting up" (engine boot,
 * measured at about 2 minutes on silver with the analysis worker busy). */
export const STARTUP_GRACE_MS = 120_000;

/** Consecutive failures before a liveness dot turns red. At the 2.5 s ping
 * cadence this is about 7.5 s of silence after the boot window. */
export const FAILURES_BEFORE_DOWN = 3;

export type ProbeOutcome =
  { ok: true } | { ok: false; timedOut: boolean; why: string };

export type LivenessSubject = "engine" | "dev server";

export type LivenessTracker = {
  /** Fold one probe outcome into the dot to paint. */
  observe: (outcome: ProbeOutcome, nowMs: number) => LibraryHealthDot;
};

export function createLivenessTracker(
  label: "Frontend" | "Backend",
  subject: LivenessSubject,
  startedAtMs: number,
): LivenessTracker {
  let failures = 0;
  let answeredOnce = false;
  return {
    observe(outcome, nowMs) {
      if (outcome.ok) {
        failures = 0;
        answeredOnce = true;
        return { label, state: "complete", detail: `${subject} online` };
      }
      failures += 1;
      const booting = !answeredOnce && nowMs - startedAtMs < STARTUP_GRACE_MS;
      if (outcome.timedOut && booting) {
        return {
          label,
          state: "waiting",
          detail: `${subject} starting up - busy booting, retrying (${outcome.why})`,
        };
      }
      if (failures < FAILURES_BEFORE_DOWN) {
        return {
          label,
          state: "waiting",
          detail: `${subject} slow to answer - retrying, ${failures} of ${FAILURES_BEFORE_DOWN} tries missed (${outcome.why})`,
        };
      }
      return {
        label,
        state: "error",
        detail: `${subject} not answering - ${outcome.why} (${failures} tries in a row)`,
      };
    },
  };
}

/** The packaged app's page row: the engine serves the UI, so it mirrors the Backend dot. */
export function appUiDot(backend: LibraryHealthDot): LibraryHealthDot {
  const label = "App UI";
  if (backend.state === "complete") {
    return {
      label,
      state: "complete",
      detail: "served by the engine, which is answering",
    };
  }
  if (backend.state === "waiting") {
    return {
      label,
      state: "waiting",
      detail: "served by the engine, which is starting up or busy",
    };
  }
  if (backend.state === "error") {
    return {
      label,
      state: "error",
      detail: "served by the engine, which is not answering",
    };
  }
  return { label, state: backend.state, detail: "served by the engine" };
}

/** A coverage read that timed out: amber "still counting", or the painted verdict kept and marked. */
export function coverageTimeoutDot(
  previous: LibraryHealthDot,
  label: LibraryHealthDot["label"],
  why: string,
): LibraryHealthDot {
  const hasVerdict =
    previous.state === "complete" ||
    previous.state === "incomplete" ||
    previous.state === "error";
  if (hasVerdict) {
    const base = previous.detail.replace(
      / \(re-check timed out, retrying\)$/,
      "",
    );
    return {
      label,
      state: previous.state,
      detail: `${base} (re-check timed out, retrying)`,
    };
  }
  return {
    label,
    state: "waiting",
    detail: `still counting - the engine is busy and has not answered yet (${why}); retrying`,
  };
}
