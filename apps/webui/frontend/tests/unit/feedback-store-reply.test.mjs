/**
 * addReply (issue #905): POST /comments/{id}/replies without a second pin.
 */

import assert from "node:assert/strict";
import { after, afterEach, before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

const API_BASE = "https://feedback-reply-api.example.test";

let store;
let originalFetch;

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

before(async () => {
  store = await loadTypeScriptModule("src/lib/rb/feedback-store.svelte.ts", {
    viteApiBase: API_BASE,
  });
  originalFetch = globalThis.fetch;
});

after(() => {
  globalThis.fetch = originalFetch;
});

afterEach(() => {
  store.feedbackState.availability = "unknown";
  store.feedbackState.pins = [];
  store.feedbackState.error = null;
  store.feedbackState.pinSummary = null;
  store.feedbackState.pinSummaryError = null;
});

test("addReply POSTs the replies path with operator author", async () => {
  const pin = {
    id: "abc123",
    text: "opening",
    x_pct: 1,
    y_pct: 2,
    page: "/performance",
    created_at: "2026-09-02T09:01:16Z",
    build: { git_sha: "sha" },
    replies: [{ id: "r1", author: "operator", text: "follow-up", created_at: "t" }],
  };
  let seen;
  let summaryGets = 0;
  globalThis.fetch = async (request) => {
    // A successful reply also refreshes the operator summary (PR #4094 Sol P2).
    if (new URL(request.url).pathname === "/api/v1/feedback/comments/summary") {
      summaryGets++;
      const zero = { total: 0, blocked: 0, fixed: 0, merged: 0, harvested: 0 };
      return jsonResponse({
        operator: { ...zero, sent_to_queue: 0, in_progress: 0, delegated: 0 },
        lifecycle: { ...zero, untriaged: 0, open: 0, issued: 0 },
        fleet_correlation: "ok",
      });
    }
    seen = request;
    return jsonResponse(pin);
  };

  store.feedbackState.availability = "ok";
  store.feedbackState.pins = [{ ...pin, replies: [] }];
  const out = await store.addReply("abc123", "follow-up");

  assert.equal(seen.url, `${API_BASE}/api/v1/feedback/comments/abc123/replies`);
  assert.equal(seen.method, "POST");
  assert.deepEqual(await seen.json(), { text: "follow-up", author: "operator" });
  assert.equal(out?.id, "abc123");
  assert.equal(store.feedbackState.pins.length, 1);
  assert.equal(store.feedbackState.pins[0].replies.length, 1);
  assert.equal(store.feedbackState.error, null);
  assert.equal(summaryGets, 1, "a successful reply refreshes the summary once");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(store.feedbackState.pinSummaryError, null, "the fixture is a valid summary");
});

test("addReply failure leaves pins unchanged and sets error", async () => {
  globalThis.fetch = async () => jsonResponse({ detail: "nope" }, 500);

  store.feedbackState.availability = "ok";
  const before = [{ id: "abc123", text: "opening", replies: [] }];
  store.feedbackState.pins = before.slice();
  const out = await store.addReply("abc123", "follow-up");

  assert.equal(out, null);
  assert.equal(store.feedbackState.pins.length, 1);
  assert.equal(store.feedbackState.pins[0].replies.length, 0);
  assert.ok(store.feedbackState.error);
});
