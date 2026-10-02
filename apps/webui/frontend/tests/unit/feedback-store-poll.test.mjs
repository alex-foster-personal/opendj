/**
 * Same-tab pin polling (issue #914 review, Wed 2 Sep 2026): hydrateFeedback
 * runs once and never again, so without a refresh loop an agent's PATCH
 * while the reviewer keeps the app open never appears until a full page
 * reload. Mirrors usb-tracker-api.test.mjs's fetch-mock convention.
 *
 * Regression lines:
 * - if refreshPins fetches before the store is known 'ok' then a probe still
 *   in flight (or a daemon with no feedback API) gets hit by a live poll
 * - if refreshPins does not overwrite feedbackState.pins with the response
 *   then a PATCH lands on the daemon but this tab never sees it
 * - if startPinWatch can be called twice and schedule two timers then a
 *   remount doubles the poll rate forever
 * - if stopPinWatch leaves the timer running then the poll outlives the
 *   component and leaks
 */

import assert from "node:assert/strict";
import { after, afterEach, before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

const API_BASE = "https://feedback-poll-api.example.test";

let store;
let originalFetch;
let originalSetInterval;
let originalClearInterval;

function jsonResponse(body) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

const EMPTY_PIN_SUMMARY = {
  operator: {
    total: 0,
    sent_to_queue: 0,
    in_progress: 0,
    delegated: 0,
    fixed: 0,
    merged: 0,
    blocked: 0,
    harvested: 0,
  },
  lifecycle: {
    total: 0,
    untriaged: 0,
    open: 0,
    issued: 0,
    blocked: 0,
    fixed: 0,
    merged: 0,
    harvested: 0,
  },
  fleet_correlation: "ok",
};

function feedbackPathname(request) {
  const { pathname } = new URL(request.url);
  return pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

function mockFeedbackFetch(handlers) {
  return async (request) => {
    const pathname = feedbackPathname(request);
    if (pathname === "/api/v1/feedback/comments/summary") {
      return handlers.summary?.() ?? jsonResponse(EMPTY_PIN_SUMMARY);
    }
    if (pathname === "/api/v1/feedback/comments") {
      return handlers.comments(request);
    }
    throw new Error(`unexpected feedback poll fetch: ${pathname}`);
  };
}

before(async () => {
  store = await loadTypeScriptModule("src/lib/rb/feedback-store.svelte.ts", {
    viteApiBase: API_BASE,
  });
  originalFetch = globalThis.fetch;
  originalSetInterval = globalThis.setInterval;
  originalClearInterval = globalThis.clearInterval;
});

after(() => {
  globalThis.fetch = originalFetch;
  globalThis.setInterval = originalSetInterval;
  globalThis.clearInterval = originalClearInterval;
});

afterEach(() => {
  store.stopPinWatch();
  store.feedbackState.pinSummary = null;
  store.feedbackState.pinSummaryError = null;
  store.feedbackState.availability = "unknown";
  store.feedbackState.pins = [];
});

// ----- refreshPins ----------------------------------------------------------
test("refreshPins does nothing before the store is known 'ok'", async () => {
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    return jsonResponse({ comments: [] });
  };

  store.feedbackState.availability = "unknown";
  await store.refreshPins();

  assert.equal(called, false, "a poll before hydration is a wasted, premature request");
});

test("refreshPins GETs comments and replaces feedbackState.pins", async () => {
  let seen;
  const fresh = [{ id: "abc123", status: "fixed" }];
  globalThis.fetch = mockFeedbackFetch({
    comments: (request) => {
      seen = request;
      return jsonResponse({ comments: fresh });
    },
  });

  store.feedbackState.availability = "ok";
  store.feedbackState.pins = [{ id: "abc123", status: "open" }];
  await store.refreshPins();

  assert.equal(feedbackPathname(seen), "/api/v1/feedback/comments");
  assert.equal(seen.method, "GET");
  assert.deepEqual(store.feedbackState.pins, fresh);
});

test("refreshPins leaves the board as it was on a transient fetch failure", async () => {
  const stale = [{ id: "abc123", status: "open" }];
  globalThis.fetch = async () => {
    throw new TypeError("fetch failed");
  };

  store.feedbackState.availability = "ok";
  store.feedbackState.pins = stale;
  await store.refreshPins();

  assert.deepEqual(store.feedbackState.pins, stale, "one bad poll must not blank the board");
});

// ----- startPinWatch / stopPinWatch -----------------------------------------
test("startPinWatch schedules exactly one interval, even called twice", () => {
  let intervalCalls = 0;
  globalThis.setInterval = (...args) => {
    intervalCalls += 1;
    return originalSetInterval(...args);
  };

  store.startPinWatch();
  store.startPinWatch();

  assert.equal(intervalCalls, 1, "a second start while already watching must be a no-op");
});

test("stopPinWatch clears the timer so it does not outlive the component", () => {
  let cleared = 0;
  globalThis.clearInterval = (...args) => {
    cleared += 1;
    return originalClearInterval(...args);
  };

  store.startPinWatch();
  store.stopPinWatch();

  assert.equal(cleared, 1);

  // A second stop is a no-op, not a second clearInterval call.
  store.stopPinWatch();
  assert.equal(cleared, 1);
});

test("the poll tick calls refreshPins", async () => {
  let commentFetches = 0;
  globalThis.fetch = mockFeedbackFetch({
    comments: () => {
      commentFetches += 1;
      return jsonResponse({ comments: [] });
    },
  });
  let tick;
  globalThis.setInterval = (fn) => {
    tick = fn;
    return 1;
  };

  store.feedbackState.availability = "ok";
  store.startPinWatch();
  assert.ok(tick, "if setInterval was never called then this guard asserts nothing");
  await tick();

  assert.equal(commentFetches, 1);
});

test("refreshPins discards a snapshot that started before a local mutation landed", async () => {
  // GET starts, then (before it resolves) archivePin's own POST resolves and
  // updates feedbackState.pins directly. The GET's response reflects the
  // PRE-archive board, so applying it after would resurrect the archived pin.
  let resolveGet;
  globalThis.fetch = async (request) => {
    const pathname = feedbackPathname(request);
    if (pathname === "/api/v1/feedback/comments/summary") {
      return jsonResponse(EMPTY_PIN_SUMMARY);
    }
    if (request.method === "GET" && pathname === "/api/v1/feedback/comments") {
      return new Promise((resolve) => {
        resolveGet = () => resolve(jsonResponse({ comments: [{ id: "abc123", status: "fixed" }] }));
      });
    }
    return jsonResponse({
      archived_to: "archive-x.json",
      comment: { id: "abc123", status: "archived" },
    });
  };

  store.feedbackState.availability = "ok";
  store.feedbackState.pins = [{ id: "abc123", status: "fixed" }];

  const pending = store.refreshPins();
  await store.archivePin("abc123");
  assert.deepEqual(store.feedbackState.pins, [], "the archive must apply immediately");

  resolveGet();
  await pending;

  assert.deepEqual(
    store.feedbackState.pins,
    [],
    "a GET that started before the archive must not resurrect the archived pin",
  );
});

test("refreshPins retires the watch when the daemon answers 404", async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: { code: "NOT_FOUND", message: "no route" } }), {
      status: 404,
      headers: { "content-type": "application/json" },
    });

  store.feedbackState.availability = "ok";
  await store.refreshPins();

  assert.equal(
    store.feedbackState.availability,
    "missing",
    "a 404 mid-session means the daemon no longer serves feedback, same as at hydrate",
  );
});

// ----- submitFollowOn --------------------------------------------------------
test("submitFollowOn POSTs the dedicated endpoint and appends the returned pin", async () => {
  let seen;
  const child = { id: "child1", status: "open", text: "Follow-on to abc123: still flaky" };
  globalThis.fetch = async (request) => {
    seen = { url: request.url, method: request.method, body: await request.clone().json() };
    return new Response(JSON.stringify(child), {
      status: 201,
      headers: { "content-type": "application/json" },
    });
  };

  store.feedbackState.pins = [];
  const ok = await store.submitFollowOn("abc123", "still flaky");

  assert.equal(ok, true);
  assert.equal(seen.url, `${API_BASE}/api/v1/feedback/comments/abc123/follow-on`);
  assert.equal(seen.method, "POST");
  assert.deepEqual(seen.body, { text: "still flaky" });
  assert.deepEqual(store.feedbackState.pins, [child]);
});

test("submitFollowOn sends null, not an empty string, for a reference-only follow-on", async () => {
  // The daemon's CommentFollowOnIn treats `text` as optional extra content;
  // an empty string would render as a trailing ": " with nothing after it.
  let seen;
  globalThis.fetch = async (request) => {
    seen = await request.clone().json();
    return new Response(JSON.stringify({ id: "child1", status: "open" }), {
      status: 201,
      headers: { "content-type": "application/json" },
    });
  };

  store.feedbackState.pins = [];
  await store.submitFollowOn("abc123", "");

  assert.deepEqual(seen, { text: null });
});

test("submitFollowOn replaces a pin a poll already inserted, instead of duplicating it", async () => {
  // The server commits the child and a same-tab refreshPins() GET picks it up
  // BEFORE this POST's own response reaches the browser (issue #914 review,
  // Wed 3 Sep 2026). Always appending would then render the same id twice.
  const child = { id: "child1", status: "open", text: "still flaky" };
  globalThis.fetch = async () =>
    new Response(JSON.stringify(child), {
      status: 201,
      headers: { "content-type": "application/json" },
    });

  store.feedbackState.pins = [{ id: "child1", status: "open", text: "" }];
  const ok = await store.submitFollowOn("abc123", "still flaky");

  assert.equal(ok, true);
  assert.deepEqual(
    store.feedbackState.pins,
    [child],
    "the returned pin must replace the poll's copy, not sit beside it",
  );
});

test("the poll tick stops itself once the daemon is known to have no feedback API", () => {
  let cleared = 0;
  globalThis.clearInterval = (...args) => {
    cleared += 1;
    return originalClearInterval(...args);
  };
  let tick;
  globalThis.setInterval = (fn) => {
    tick = fn;
    return 1;
  };

  store.feedbackState.availability = "missing";
  store.startPinWatch();
  tick();

  assert.equal(cleared, 1, "polling a daemon that answered 404 forever is pointless churn");
});

// ----- /comments/summary (FB-20, pin 6af63c5e9b7c) --------------------------
function statusResponse(status) {
  return new Response(JSON.stringify({ detail: "x" }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("pin 6af63c5e9b7c a poll stores the daemon's /comments/summary", async () => {
  const summary = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
  globalThis.fetch = mockFeedbackFetch({
    summary: () => jsonResponse(summary),
    comments: () => jsonResponse({ comments: [] }),
  });
  store.feedbackState.availability = "ok";
  await store.refreshPins();
  assert.deepEqual(store.feedbackState.pinSummary, summary);
});

test("pin 6af63c5e9b7c a transient summary failure keeps the last-known counts", async () => {
  const last = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
  globalThis.fetch = mockFeedbackFetch({
    summary: () => statusResponse(503),
    comments: () => jsonResponse({ comments: [] }),
  });
  store.feedbackState.availability = "ok";
  store.feedbackState.pinSummary = last;
  await store.refreshPins();
  assert.deepEqual(store.feedbackState.pinSummary, last, "one bad poll must not erase measured counts");
});

test("pin 6af63c5e9b7c a 404 for the summary route clears it (daemon without FB-20)", async () => {
  globalThis.fetch = mockFeedbackFetch({
    summary: () => statusResponse(404),
    comments: () => jsonResponse({ comments: [] }),
  });
  store.feedbackState.availability = "ok";
  store.feedbackState.pinSummary = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
  await store.refreshPins();
  assert.equal(store.feedbackState.pinSummary, null);
  assert.equal(store.feedbackState.availability, "ok", "only the summary route is missing, not feedback");
});

// ----- persistent summary failures surface (PR #4094 Sol P1) ----------------
function errorEnvelope(status, code, message) {
  return new Response(JSON.stringify({ detail: { code, message } }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("pin 6af63c5e9b7c a 500 unknown_pin_status drops the counts and surfaces the error", async () => {
  globalThis.fetch = mockFeedbackFetch({
    summary: () => errorEnvelope(500, "unknown_pin_status", "pin abc has status 'bogus'"),
    comments: () => jsonResponse({ comments: [] }),
  });
  store.feedbackState.availability = "ok";
  store.feedbackState.pinSummary = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
  await store.refreshPins();
  assert.equal(store.feedbackState.pinSummary, null, "stale counts must not stay on screen as if measured");
  assert.match(store.feedbackState.pinSummaryError ?? "", /HTTP 500 unknown_pin_status/);
  assert.match(store.commentPinSummaryTitle([]), /^Comment pin summary failed: HTTP 500 unknown_pin_status/);
});

test("pin 6af63c5e9b7c an undecodable summary body is surfaced, not swallowed", async () => {
  globalThis.fetch = mockFeedbackFetch({
    summary: () =>
      new Response("{not json", { status: 200, headers: { "content-type": "application/json" } }),
    comments: () => jsonResponse({ comments: [] }),
  });
  store.feedbackState.availability = "ok";
  store.feedbackState.pinSummary = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
  await store.refreshPins();
  assert.equal(store.feedbackState.pinSummary, null);
  assert.ok(store.feedbackState.pinSummaryError, "a decode failure must reach the error state");
});

test("pin 6af63c5e9b7c a body that fails to decode with a TypeError is persistent, not transient", async () => {
  // Response.json() rejects with a TypeError for a body it cannot decode (a
  // plain body labeled Content-Encoding: gzip); that must not read as an
  // unreachable daemon and keep the stale counts on screen.
  const undecodable = new ReadableStream({
    start(controller) {
      controller.error(new TypeError("Decompression failed"));
    },
  });
  globalThis.fetch = mockFeedbackFetch({
    summary: () =>
      new Response(undecodable, { status: 200, headers: { "content-type": "application/json" } }),
    comments: () => jsonResponse({ comments: [] }),
  });
  store.feedbackState.availability = "ok";
  store.feedbackState.pinSummary = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
  await store.refreshPins();
  assert.equal(store.feedbackState.pinSummary, null, "stale counts must not stay on screen");
  assert.match(store.feedbackState.pinSummaryError ?? "", /undecodable \/comments\/summary body: Decompression failed/);
});

test("pin 6af63c5e9b7c a summary-check chunk that fails to load drops the counts and says to reload", async () => {
  globalThis.fetch = mockFeedbackFetch({
    summary: () => jsonResponse({ ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" }),
    comments: () => jsonResponse({ comments: [] }),
  });
  store._setSummaryCheckLoaderForTests(() =>
    Promise.reject(new TypeError("Failed to fetch dynamically imported module")),
  );
  try {
    store.feedbackState.availability = "ok";
    store.feedbackState.pinSummary = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
    store.feedbackState.pinSummaryError = null;
    await store.refreshPins();
    assert.equal(store.feedbackState.pinSummary, null, "stale counts must not stay on screen");
    assert.match(
      store.feedbackState.pinSummaryError ?? "",
      /summary code failed to load \(Failed to fetch dynamically imported module\); reload the page/,
    );
  } finally {
    store._setSummaryCheckLoaderForTests(null);
  }
  // Control: with the real chunk loadable again, the next poll stores the
  // summary and clears the error.
  await store.refreshPins();
  assert.equal(store.feedbackState.pinSummaryError, null);
  assert.ok(store.feedbackState.pinSummary, "a later successful load must restore the counts");
});

test("pin 6af63c5e9b7c an unreachable daemon keeps the last-known counts and raises no error", async () => {
  const last = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
  globalThis.fetch = mockFeedbackFetch({
    summary: () => {
      throw new TypeError("fetch failed");
    },
    comments: () => jsonResponse({ comments: [] }),
  });
  store.feedbackState.availability = "ok";
  store.feedbackState.pinSummary = last;
  await store.refreshPins();
  assert.deepEqual(store.feedbackState.pinSummary, last);
  assert.equal(store.feedbackState.pinSummaryError, null);
});

test("pin 6af63c5e9b7c a later successful summary clears the surfaced error", async () => {
  const fresh = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
  globalThis.fetch = mockFeedbackFetch({
    summary: () => jsonResponse(fresh),
    comments: () => jsonResponse({ comments: [] }),
  });
  store.feedbackState.availability = "ok";
  store.feedbackState.pinSummaryError = "HTTP 500 unknown_pin_status: x";
  await store.refreshPins();
  assert.equal(store.feedbackState.pinSummaryError, null);
  assert.deepEqual(store.feedbackState.pinSummary, fresh);
});

// ----- summary follows local mutations (PR #4094 Sol P2) --------------------
test("pin 6af63c5e9b7c a successful addPin refreshes the operator summary at once", async () => {
  const after = {
    ...EMPTY_PIN_SUMMARY,
    operator: { ...EMPTY_PIN_SUMMARY.operator, total: 1, sent_to_queue: 1 },
    fleet_correlation: "ok",
  };
  let summaryGets = 0;
  globalThis.fetch = mockFeedbackFetch({
    summary: () => {
      summaryGets++;
      return jsonResponse(after);
    },
    comments: () => jsonResponse({ id: "new1", status: null }),
  });
  store.feedbackState.availability = "ok";
  store.feedbackState.pinSummary = { ...EMPTY_PIN_SUMMARY, fleet_correlation: "ok" };
  const created = await store.addPin({ text: "hi", x: 0, y: 0 });
  assert.equal(created?.id, "new1");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(summaryGets, 1, "a mutation must schedule exactly one summary refresh");
  assert.deepEqual(store.feedbackState.pinSummary, after);
});

test("pin 6af63c5e9b7c a failed addPin does not refresh the summary", async () => {
  let summaryGets = 0;
  globalThis.fetch = mockFeedbackFetch({
    summary: () => {
      summaryGets++;
      return jsonResponse(EMPTY_PIN_SUMMARY);
    },
    comments: () => errorEnvelope(422, "bad_pin", "no"),
  });
  store.feedbackState.availability = "ok";
  assert.equal(await store.addPin({ text: "hi", x: 0, y: 0 }), null);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(summaryGets, 0);
  store.feedbackState.error = null;
});

// ----- the summary body is validated before it is stored (PR #4094 Sol P1) --
for (const [label, body, why] of [
  ["an empty object", {}, /operator is missing/],
  [
    "a missing operator bucket",
    { ...EMPTY_PIN_SUMMARY, operator: { ...EMPTY_PIN_SUMMARY.operator, merged: undefined } },
    /operator\.merged/,
  ],
  [
    "a non-integer lifecycle bucket",
    { ...EMPTY_PIN_SUMMARY, lifecycle: { ...EMPTY_PIN_SUMMARY.lifecycle, open: "3" } },
    /lifecycle\.open/,
  ],
  ["an unknown fleet_correlation", { ...EMPTY_PIN_SUMMARY, fleet_correlation: "maybe" }, /fleet_correlation/],
]) {
  test(`pin 6af63c5e9b7c a 200 summary with ${label} is surfaced, not stored`, async () => {
    globalThis.fetch = mockFeedbackFetch({
      summary: () => jsonResponse(body),
      comments: () => jsonResponse({ comments: [] }),
    });
    store.feedbackState.availability = "ok";
    store.feedbackState.pinSummary = { ...EMPTY_PIN_SUMMARY };
    await store.refreshPins();
    assert.equal(store.feedbackState.pinSummary, null, "a malformed body must not reach the controls");
    assert.match(store.feedbackState.pinSummaryError ?? "", /malformed \/comments\/summary body/);
    assert.match(store.feedbackState.pinSummaryError ?? "", why);
  });
}
