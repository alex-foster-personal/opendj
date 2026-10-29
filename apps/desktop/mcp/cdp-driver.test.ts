// Run: pnpm --dir apps/desktop exec tsx --test mcp/cdp-driver.test.ts
// The CDP driver's pure decisions, fed plain URLs, target lists and captured
// Chromium protocol payloads. Everything that talks to a browser (sockets,
// discovery, navigation, timeouts) is tested against a real Chromium in
// cdp-driver.real.test.ts; nothing here replaces WebSocket or fetch.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { appPageProblem, evaluationValue, LoadWait, navigateReplyProblem, pickAppTarget, replyResult } from "./cdp-driver";

type Lifecycle = Record<string, unknown> & { name: string; frameId: string; loaderId: string };
type Message = { reply: { frameId: string; loaderId: string } } | { event: Lifecycle };
const captured = JSON.parse(
  readFileSync(new URL("./fixtures/cdp-lifecycle-iframe-navigate.json", import.meta.url), "utf8"),
) as { messages: Message[] };
const reply = (captured.messages.find((m) => "reply" in m) as { reply: { frameId: string; loaderId: string } }).reply;
const events = captured.messages.flatMap((m) => ("event" in m ? [m.event] : []));
const ownLoad = events.find(
  (e) => e.name === "load" && e.frameId === reply.frameId && e.loaderId === reply.loaderId,
) as Lifecycle;

/** Replays the capture through LoadWait in arrival order; the index of the event that ends the wait. */
function endsAt(messages: Message[]): number | "before-reply" | null {
  const wait = new LoadWait();
  for (const [i, m] of messages.entries()) {
    if ("reply" in m) {
      if (wait.expect(m.reply.frameId, m.reply.loaderId)) return "before-reply";
    } else if (wait.see(m.event)) return i;
  }
  return null;
}

test("the capture holds the cases a navigate must tell apart", () => {
  // Control on the fixture itself: if these are missing the tests below prove nothing.
  assert.ok(ownLoad, "the navigation's own load");
  assert.ok(
    events.some((e) => e.name === "load" && e.frameId === reply.frameId && e.loaderId !== reply.loaderId),
    "the previous page's load on the same frame",
  );
  assert.ok(events.some((e) => e.name === "load" && e.frameId !== reply.frameId), "a child frame's load");
  assert.ok(
    events.some((e) => e.name === "DOMContentLoaded" && e.frameId === reply.frameId && e.loaderId === reply.loaderId),
    "its own DOMContentLoaded",
  );
});

test("a navigate ends on its own frame's load, not another loader's, a child frame's or DOMContentLoaded", () => {
  const at = endsAt(captured.messages);
  assert.equal(at, captured.messages.findIndex((m) => "event" in m && m.event === ownLoad));
  // It is the last message: every other load in the capture arrived first and was refused.
  assert.equal(at, captured.messages.length - 1);
});

test("a load that lands before the navigate reply still counts, if it is ours", () => {
  // Never observed in 80 real navigations (the reply always came first), so this
  // replays the same captured payloads with the own load moved ahead of the reply.
  const replyAt = captured.messages.findIndex((m) => "reply" in m);
  const reordered = captured.messages.filter((m) => !("event" in m && m.event === ownLoad));
  reordered.splice(replyAt, 0, { event: ownLoad });
  assert.equal(endsAt(reordered), "before-reply");
  // Control: an early load of ANOTHER loader is buffered but never accepted.
  const withoutOwn = captured.messages.filter((m) => !("event" in m && m.event === ownLoad));
  assert.equal(endsAt(withoutOwn), null);
});

test("only the app origin counts as ready", () => {
  assert.equal(
    appPageProblem("http://127.0.0.1/performance", "http://127.0.0.1"),
    null,
  );
  assert.match(
    String(appPageProblem("http://localhost/", "http://127.0.0.1")),
    /not the app/,
  );
  assert.match(
    String(appPageProblem("https://example.com/", "http://127.0.0.1")),
    /not the app/,
  );
  assert.match(
    String(appPageProblem("chrome-error://chromewebdata/", "http://127.0.0.1")),
    /not the app/,
  );
  assert.match(
    String(appPageProblem("opendj://app/index.html", "http://127.0.0.1")),
    /bootstrap/,
  );
  // Another loopback page (a popup, an unrelated local server) is never the app.
  assert.match(
    String(appPageProblem("http://[::1]/", "http://127.0.0.1")),
    /not the app/,
  );
  // Control: the same origin on any path is ready.
  assert.equal(appPageProblem("http://127.0.0.1/library?x=1", "http://127.0.0.1"), null);
});

test("the target is picked by the app origin, never a popup that merely speaks http", () => {
  const at = (id: string, url: string) => ({
    id,
    type: "page",
    url,
    title: "",
    webSocketDebuggerUrl: `ws://${id}`,
  });
  const popup = at("popup", "https://accounts.example.com/");
  const app = at("app", "http://127.0.0.1/performance");
  const boot = at("boot", "opendj://app/index.html");
  assert.equal(pickAppTarget([popup, app], "http://127.0.0.1")?.id, "app");
  assert.equal(
    pickAppTarget([popup, boot], "http://127.0.0.1")?.id,
    "boot",
    "the bootstrap while the app boots",
  );
  assert.equal(
    pickAppTarget([popup], "http://127.0.0.1"),
    null,
    "if a lone popup is driven then broken",
  );
  // Another loopback origin is not the app either.
  const local = at("local", "http://[::1]/");
  assert.equal(pickAppTarget([local, app], "http://127.0.0.1")?.id, "app");
  assert.equal(pickAppTarget([local, boot], "http://127.0.0.1")?.id, "boot");
  // A window that started while Vite was down is on Chromium's error page:
  // attachable, so navigate can recover it, yet never preferred over the app.
  const failed = at("failed", "chrome-error://chromewebdata/");
  assert.equal(
    pickAppTarget([popup, failed], "http://127.0.0.1")?.id,
    "failed",
  );
  assert.equal(pickAppTarget([failed, app], "http://127.0.0.1")?.id, "app");
  assert.equal(pickAppTarget([failed, boot], "http://127.0.0.1")?.id, "boot");
  // The window this session drove, now off-origin, beats an unrelated error
  // page; two error pages are ambiguous and neither is driven (Sol P1 on #6830).
  const driven = at("driven", "https://example.com/elsewhere");
  assert.equal(
    pickAppTarget([failed, driven], "http://127.0.0.1", "driven")?.id,
    "driven",
  );
  const failed2 = at("failed2", "chrome-error://chromewebdata/");
  assert.equal(pickAppTarget([failed, failed2], "http://127.0.0.1"), null);
  assert.equal(
    pickAppTarget([failed, failed2], "http://127.0.0.1", "failed2")?.id,
    "failed2",
  );
  // An app page still wins over the known target.
  assert.equal(
    pickAppTarget([driven, app], "http://127.0.0.1", "driven")?.id,
    "app",
  );
});

test("an evaluation result is read only when it carries a value or is undefined (Sol P1 on #6830)", () => {
  assert.equal(evaluationValue({ type: "string", value: "x" }), "x");
  // Present falsy values pass through; only type "undefined" maps to null.
  assert.equal(evaluationValue({ type: "number", value: 0 }), 0);
  assert.equal(evaluationValue({ type: "object", subtype: "null", value: null }), null);
  assert.equal(evaluationValue({ type: "undefined" }), null);
  assert.throws(() => evaluationValue(undefined), /no result object/);
  assert.throws(() => evaluationValue({ type: "object" }), /object with no value/);
  assert.throws(
    () => evaluationValue({ type: "number", unserializableValue: "NaN" }),
    /returned NaN/,
  );
});

test("a CDP reply is read only when it carries a result object (Sol P1 on #6830)", () => {
  // Control: the empty result real Chromium sends for Page.enable passes through.
  assert.deepEqual(replyResult("Page.enable", { id: 1, result: {} }), {});
  assert.deepEqual(replyResult("Page.navigate", { id: 2, result: reply }), reply);
  assert.throws(() => replyResult("Page.navigate", { id: 3 }), /Page\.navigate replied with no result object/);
  assert.throws(
    () => replyResult("Page.navigate", { id: 4, result: null as unknown as Record<string, unknown> }),
    /no result object/,
  );
});

test("a Page.navigate reply is trusted only with a frameId, even with no loader (Sol P1 on #6830)", () => {
  assert.match(navigateReplyProblem({}) ?? "", /no frameId/, "an empty result is a contract failure, not a hash change");
  assert.match(navigateReplyProblem({ loaderId: "L" }) ?? "", /no frameId/);
  assert.match(navigateReplyProblem({ frameId: "F", loaderId: 7 }) ?? "", /malformed loaderId/);
  assert.match(navigateReplyProblem({ frameId: "F", errorText: "net::ERR_ABORTED" }) ?? "", /failed: net::ERR_ABORTED/);
  // Controls: a same-document reply (frameId, no loader) and a full one are accepted.
  assert.equal(navigateReplyProblem({ frameId: "F" }), null);
  assert.equal(navigateReplyProblem({ frameId: "F", loaderId: "L" }), null);
});
