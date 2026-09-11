/**
 * `addPinWithAttachment`'s three outcomes (PR #1425 P1 review fixup,
 * BLOCKING: apps/webui/frontend/src/lib/components/rb/FeedbackPinDraftBubble.svelte:126).
 *
 * Before this fix the function returned `{ok: false, attachmentError: ""}`
 * for BOTH a failed attachment upload (pin created, screenshot lost) AND a
 * failed pin create (nothing created at all) - the caller could not tell
 * them apart, so FeedbackPinDraftBubble.svelte hard-coded `saved = true` for
 * both and cleared the draft even when nothing was ever saved to the
 * server, showing a false "pin saved" toast with an empty reason.
 *
 * Regression lines:
 * - if a failed pin create is still reported as `created` or
 *   `created-attachment-failed` then a caller clears the draft over a pin
 *   that does not exist on the server, and the operator's typed comment is
 *   gone with no way to retry it
 * - if a failed pin create's `reason` reads as an empty string then the
 *   caller has nothing to show the operator beyond "it failed"
 * - if a failed attachment upload (pin DID get created) is confused with a
 *   failed create then a caller wrongly keeps retrying Save, creating a
 *   second pin for the same text
 * - if the attachment upload is attempted after a failed create then a
 *   multipart POST fires with no valid comment id to attach to
 */

import assert from "node:assert/strict";
import { afterEach, before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

const API_BASE = "https://feedback-attachment-outcome-api.example.test";

let store;
let originalFetch;

before(async () => {
  store = await loadTypeScriptModule("src/lib/rb/feedback-store.svelte.ts", {
    viteApiBase: API_BASE,
  });
  originalFetch = globalThis.fetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  store.feedbackState.pins = [];
  store.feedbackState.error = null;
});

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const pinBody = {
  x_pct: 10,
  y_pct: 20,
  anchor: ".bank",
  page: "/performance",
  text: "the header icon is misaligned",
  ui: "chrome-loop",
  viewport_width: 1280,
  viewport_height: 800,
  author: "operator",
};

function fakeFile() {
  return new File([new Uint8Array(100)], "shot.png", { type: "image/png" });
}

test("both the create and the upload succeed: reports created, and the pin lands in the store", async () => {
  const created = { id: "abc123", ...pinBody, status: "open" };
  const attached = { ...created, attachment: { id: "att1", content_type: "image/png", size_bytes: 100, url: "/x" } };
  let posts = 0;
  globalThis.fetch = async (request) => {
    posts += 1;
    if (posts === 1) return jsonResponse(created, 201);
    return jsonResponse(attached, 201);
  };

  const result = await store.addPinWithAttachment(pinBody, fakeFile());

  assert.deepEqual(result, { kind: "created" });
  assert.equal(posts, 2, "create then upload - exactly two requests");
  assert.deepEqual(store.feedbackState.pins, [attached]);
});

test("the create succeeds but the upload fails: reports created-attachment-failed with the real reason, pin stays in the store", async () => {
  const created = { id: "abc123", ...pinBody, status: "open" };
  let posts = 0;
  globalThis.fetch = async (request) => {
    posts += 1;
    if (posts === 1) return jsonResponse(created, 201);
    return jsonResponse(
      { detail: { code: "ATTACHMENT_TOO_LARGE", message: "attachment exceeds the 8388608 byte limit" } },
      413,
    );
  };

  const result = await store.addPinWithAttachment(pinBody, fakeFile());

  assert.equal(result.kind, "created-attachment-failed");
  assert.match(result.reason, /8388608/);
  assert.deepEqual(
    store.feedbackState.pins,
    [created],
    "the pin itself must still exist - only the attachment failed",
  );
});

test("the create itself fails: reports create-failed with a real reason, no upload is ever attempted, nothing lands in the store", async () => {
  let posts = 0;
  globalThis.fetch = async () => {
    posts += 1;
    return jsonResponse({ detail: { code: "SERVER_ERROR", message: "daemon is unreachable" } }, 500);
  };

  const result = await store.addPinWithAttachment(pinBody, fakeFile());

  assert.equal(result.kind, "create-failed");
  assert.equal(result.reason, "daemon is unreachable");
  assert.notEqual(result.reason, "", "an empty reason leaves the operator nothing to act on");
  assert.equal(posts, 1, "a failed create must never be followed by an attachment upload POST");
  assert.deepEqual(store.feedbackState.pins, [], "nothing was created, so nothing should appear in the store");
});

// ----- submitFollowOnWithAttachment (PR #1425 P1, round 3) ------------------
// `handleSavePinDraft`'s follow-on branch used to return through
// `submitFollowOn` alone before ever reaching the attachment logic below it,
// so a screenshot pasted into a follow-on draft was staged in the UI
// ("screenshot: ... - attached on Save") and then silently discarded on
// Save. `submitFollowOnWithAttachment` is the follow-on twin of
// `addPinWithAttachment` that the fixed branch now calls whenever a
// follow-on save has a pending attachment - these tests prove IT actually
// uploads, mirroring the three `addPinWithAttachment` outcomes above.
test("follow-on: the create and the upload both succeed - reports created, and the uploaded pin lands in the store", async () => {
  const created = { id: "child1", ...pinBody, status: "open" };
  const attached = {
    ...created,
    attachment: { id: "att1", content_type: "image/png", size_bytes: 100, url: "/x" },
  };
  const calls = [];
  globalThis.fetch = async (request) => {
    calls.push(request instanceof Request ? request.url : String(request));
    if (calls.length === 1) return jsonResponse(created, 201);
    return jsonResponse(attached, 201);
  };

  const result = await store.submitFollowOnWithAttachment("parent1", "still flaky", fakeFile());

  assert.deepEqual(result, { kind: "created" });
  assert.equal(calls.length, 2, "follow-on create then attachment upload - exactly two requests");
  assert.ok(
    calls[0].includes("/follow-on"),
    "the first request must be the dedicated follow-on endpoint, not a plain comment create",
  );
  assert.ok(
    calls[1].includes(`/comments/${created.id}/attachment`),
    "the upload must target the id the follow-on create just returned",
  );
  assert.deepEqual(store.feedbackState.pins, [attached]);
});

test("follow-on: the create succeeds but the upload fails - reports created-attachment-failed, the follow-on pin stays in the store", async () => {
  const created = { id: "child2", ...pinBody, status: "open" };
  let posts = 0;
  globalThis.fetch = async () => {
    posts += 1;
    if (posts === 1) return jsonResponse(created, 201);
    return jsonResponse(
      { detail: { code: "ATTACHMENT_TOO_LARGE", message: "attachment exceeds the 8388608 byte limit" } },
      413,
    );
  };

  const result = await store.submitFollowOnWithAttachment("parent1", "still flaky", fakeFile());

  assert.equal(result.kind, "created-attachment-failed");
  assert.match(result.reason, /8388608/);
  assert.deepEqual(
    store.feedbackState.pins,
    [created],
    "the follow-on pin itself must still exist - only the attachment failed",
  );
});

test("follow-on: the create itself fails - reports create-failed, no upload is ever attempted", async () => {
  let posts = 0;
  globalThis.fetch = async () => {
    posts += 1;
    return jsonResponse({ detail: { code: "SERVER_ERROR", message: "daemon is unreachable" } }, 500);
  };

  const result = await store.submitFollowOnWithAttachment("parent1", "still flaky", fakeFile());

  assert.equal(result.kind, "create-failed");
  assert.equal(result.reason, "daemon is unreachable");
  assert.equal(posts, 1, "a failed follow-on create must never be followed by an attachment upload POST");
  assert.deepEqual(store.feedbackState.pins, []);
});
