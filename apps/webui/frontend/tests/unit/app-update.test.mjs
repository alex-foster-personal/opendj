/**
 * INSTALL-40: the "Restart to update" nudge, its logic and its RENDERED markup.
 *
 * The component is compiled and rendered with svelte's own server renderer
 * (no DOM in this suite, by design), so these prove what markup the nudge
 * produces for a given record. The poll and the click are not exercised here:
 * the click's request is `requestInstall`, tested directly below, and the
 * shell half is tests/unit/shell-commands.test.mjs plus the Rust tests.
 *
 * Regression lines:
 * - if the nudge renders when nothing is pending then it nags about nothing
 * - if the tooltip omits the short sha then nobody can tell WHICH build installs
 * - if a 409 from install reads as accepted then a refused install looks started
 * - if installLocalUpdate resolves installed outside Tauri then a browser tab
 *   claims an install it cannot do
 */
import assert from "node:assert/strict";
import { before, test } from "node:test";

import { loadSvelteSsrModule } from "./load-svelte-ssr.mjs";
import { loadTypeScriptModule } from "./load-typescript.mjs";

const ENTRY = [
  "export { default as Nudge } from '$lib/components/rb/RestartToUpdate.svelte';",
  "export { render } from 'svelte/server';",
].join("\n");

const PENDING = {
  state: "pending",
  pending: {
    artifact: "/x/bundle/macos/Open DJ.app.tar.gz",
    app_version: "1.0.0-alpha.1",
    git_sha_full: "eaf12c7a0a1b178168a1420fe495048959e4afc7",
    git_sha: "eaf12c7a",
    built_at_utc: "2026-11-03T10:00:00Z",
    release_channel: "local-unnotarized",
    notes: "set fix: jog wheel",
    flagged_at_utc: "2026-11-03T10:05:00Z",
    size_bytes: 487262664,
    sha256: "a".repeat(64),
  },
  running_git_sha_full: "0d41a28c0000000000000000000000000000beef",
  running_app_version: "1.0.0-alpha.1",
  detail: "build eaf12c7a is ready; restart to update",
};

let logic;
let ssr;

before(async () => {
  logic = await loadTypeScriptModule("src/lib/rb/app-update.ts");
  ssr = await loadSvelteSsrModule(ENTRY);
});

function render(initial) {
  return ssr.render(ssr.Nudge, { props: { initial } }).body;
}

test("only a pending build yields a nudge", () => {
  assert.equal(logic.summarizePending(null), null);
  for (const state of ["none", "installed", "artifact-changed"]) {
    assert.equal(logic.summarizePending({ ...PENDING, state }), null, state);
  }
  const nudge = logic.summarizePending(PENDING);
  assert.equal(nudge.label, "Restart to update");
  assert.match(nudge.title, /eaf12c7a/);
  assert.match(nudge.title, /v1\.0\.0-alpha\.1/);
  assert.match(nudge.title, /2026-11-03T10:00:00Z/);
  assert.match(nudge.title, /set fix: jog wheel/);
});

test("the component renders the button only when a build is pending", () => {
  assert.doesNotMatch(render(null), /restart-to-update/);
  assert.doesNotMatch(
    render({ ...PENDING, state: "installed" }),
    /restart-to-update/,
  );
  const html = render(PENDING);
  assert.match(html, /data-testid="restart-to-update"/);
  assert.match(html, />\s*Restart to update\s*</);
  assert.match(html, /title="[^"]*eaf12c7a/);
});

function jsonFetch(status, body, calls = []) {
  return async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  };
}

test("requestInstall posts once and reads 202 as accepted", async () => {
  const calls = [];
  const outcome = await logic.requestInstall(
    jsonFetch(
      202,
      { command_id: "c1", git_sha_full: PENDING.pending.git_sha_full },
      calls,
    ),
    "",
  );
  assert.deepEqual(outcome, { kind: "accepted", commandId: "c1" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/app-update/install");
  assert.equal(calls[0].init.method, "POST");
});

test("a refused install carries the engine code and detail", async () => {
  const outcome = await logic.requestInstall(
    jsonFetch(409, { error: "artifact-changed", detail: "flag it again" }),
    "",
  );
  assert.equal(outcome.kind, "refused");
  assert.match(outcome.reason, /artifact-changed: flag it again/);
});

test("a status the engine cannot give hides the nudge", async () => {
  assert.equal(
    await logic.fetchAppUpdateStatus(
      jsonFetch(404, { detail: "Not Found" }),
      "",
    ),
    null,
  );
  assert.equal(
    await logic.fetchAppUpdateStatus(async () => {
      throw new Error("down");
    }, ""),
    null,
  );
  assert.equal(
    (await logic.fetchAppUpdateStatus(jsonFetch(200, PENDING), "")).state,
    "pending",
  );
});

test("installLocalUpdate refuses outside the Tauri shell", async () => {
  const outcome = await logic.installLocalUpdate(
    "http://127.0.0.1:1/api/v1/app-update/manifest.json",
    {},
  );
  assert.equal(outcome.kind, "refused");
  assert.match(outcome.reason, /Tauri desktop shell/);
});

// INSTALL-8338b (the maintainer, Tue 3 Nov 2026: "it still says restart to
// update after restarting so super unclear if it updated or not").
// - if the relaunched app still shows the nudge for the build it runs then
//   nobody can tell the update worked -> broken
// - if "Updated to <sha>" shows again after this viewer saw it then it nags -> broken
// - if the click has no in-flight face then a working install looks stuck -> broken

test("the running flagged build reads as Updated to its sha, once", () => {
  const installed = { ...PENDING, state: "installed" };
  const confirmed = logic.summarizeInstalled(installed, null);
  assert.equal(confirmed.label, "Updated to eaf12c7a");
  assert.match(confirmed.title, /eaf12c7a/);
  assert.equal(logic.summarizeInstalled(installed, PENDING.pending.git_sha_full), null);
  assert.equal(logic.summarizeInstalled(PENDING, null), null);
  assert.equal(logic.summarizeInstalled(null, null), null);
});

test("the relaunched app renders the confirmation, not the nudge", () => {
  const html = render({ ...PENDING, state: "installed" });
  assert.match(html, /data-testid="app-updated"/);
  assert.match(html, /Updated to eaf12c7a/);
  assert.doesNotMatch(html, /restart-to-update/);
});

test("the click shows installing, then restarting", () => {
  assert.equal(logic.installPhaseLabel("idle", "Restart to update"), "Restart to update");
  assert.equal(logic.installPhaseLabel("installing", "Restart to update"), "Installing update...");
  assert.equal(logic.installPhaseLabel("restarting", "Restart to update"), "Restarting...");
});

// INSTALL-43 (the maintainer, Tue 3 Nov 2026): every install counts down 10 s in the
// app, even mid-play, and the only cancel is Esc Space Esc Space Esc Space Esc Space.
// - if Esc Space x3 cancels then a DJ fumbling two keys aborts a wanted update -> broken
// - if a wrong key does not restart the combo then any key mash can cancel -> broken
// - if the countdown still shows once the shell claimed the order then it lies -> broken

const COUNTING = {
  ...PENDING,
  install: {
    command_id: "c9",
    state: "pending",
    outcome: null,
    error: null,
    countdown_ends_at_utc: "2026-11-03T10:06:10.000Z",
    countdown_remaining_s: 9.4,
  },
};

function press(keys) {
  let progress = 0;
  for (const key of keys) progress = logic.advanceCancelCombo(progress, key);
  return progress;
}

test("exactly Esc Space four times completes the cancel combo", () => {
  const combo = ["Escape", " ", "Escape", " ", "Escape", " ", "Escape", " "];
  assert.equal(logic.comboComplete(press(combo)), true);
  assert.equal(logic.comboComplete(press(combo.slice(0, 6))), false);
  assert.equal(logic.comboComplete(press(["Escape", " ", "a", ...combo.slice(2)])), false);
  assert.equal(logic.comboComplete(press(["Escape", "Escape", ...combo.slice(1)])), true);
  assert.equal(logic.advanceCancelCombo(3, " ", true), 3);
  assert.equal(logic.isComboKey(" "), true);
  assert.equal(logic.isComboKey("Enter"), false);
});

test("an install counting down shows the overlay text, and ticks between polls", () => {
  const countdown = logic.summarizeCountdown(COUNTING);
  assert.equal(countdown.text, "Updating to eaf12c7a in 10 s. Press Esc Space ×4 to cancel.");
  assert.equal(countdown.commandId, "c9");
  assert.equal(logic.summarizeCountdown(COUNTING, 3000).secondsLeft, 7);
  assert.match(logic.summarizeCountdown(COUNTING, 20_000).text, /Updating to eaf12c7a now/);
  const claimed = { ...COUNTING, install: { ...COUNTING.install, state: "claimed", countdown_remaining_s: null } };
  assert.equal(logic.summarizeCountdown(claimed), null);
  assert.equal(logic.summarizeCountdown(PENDING), null);
  const cancelled = { ...COUNTING, install: { ...COUNTING.install, state: "failed", outcome: "cancelled" } };
  assert.equal(logic.summarizeCountdown(cancelled), null);
  assert.equal(logic.installWasCancelled(cancelled), true);
});

test("the tray renders the countdown overlay while an install waits", () => {
  const html = render(COUNTING);
  assert.match(html, /data-testid="install-countdown"/);
  assert.match(html, /Updating to eaf12c7a in 10 s\. Press Esc Space ×4 to cancel\./);
  assert.doesNotMatch(render(PENDING), /install-countdown/);
});

test("requestCancel posts the reason and reads a refusal", async () => {
  const calls = [];
  const ok = await logic.requestCancel("Esc Space x4 in the app", jsonFetch(200, { cancelled: true, command_id: "c9" }, calls), "");
  assert.deepEqual(ok, { kind: "accepted", commandId: "c9" });
  assert.equal(calls[0].url, "/api/v1/app-update/cancel");
  assert.equal(JSON.parse(calls[0].init.body).reason, "Esc Space x4 in the app");
  const late = await logic.requestCancel("x", jsonFetch(409, { error: "already-installing", detail: "claimed" }), "");
  assert.equal(late.kind, "refused");
  assert.match(late.reason, /already-installing/);
});

// INSTALL-44 (Tue 3 Nov 2026: a genre-keys build silently dropped the set fixes).
// - if a not-descended install shows no loud warning then commits vanish silently -> broken
const DROPPING = {
  ...COUNTING,
  pending: {
    ...COUNTING.pending,
    descent: {
      verdict: "not-descended",
      running_git_sha_full: PENDING.running_git_sha_full,
      previous_git_sha_full: PENDING.running_git_sha_full,
      missing_commits: 328,
      missing_from: "af--set-rc",
      warning: "NOT DESCENDED FROM YOUR LAST BUILD: missing 328 commits from af--set-rc",
    },
  },
};

test("a build that drops the running build's commits warns loudly in the nudge and the countdown", () => {
  assert.deepEqual(logic.descentWarning(DROPPING), {
    text: "NOT DESCENDED FROM YOUR LAST BUILD: missing 328 commits from af--set-rc",
    loud: true,
  });
  assert.equal(logic.descentWarning(PENDING), null);
  assert.match(logic.summarizePending({ ...DROPPING, install: null }).title, /NOT DESCENDED FROM YOUR LAST BUILD/);
  assert.match(logic.summarizeCountdown(DROPPING).warning, /missing 328 commits from af--set-rc/);
  const html = render(DROPPING);
  assert.match(html, /data-testid="install-descent-warning"[^>]*>NOT DESCENDED FROM YOUR LAST BUILD: missing 328 commits from af--set-rc/);
  assert.match(html, /data-testid="update-descent-warning"/);
  const unverified = { ...DROPPING, pending: { ...DROPPING.pending, descent: { ...DROPPING.pending.descent, verdict: "unverified", warning: "descent from your last build is unverified" } } };
  assert.equal(logic.descentWarning(unverified).loud, false);
});
