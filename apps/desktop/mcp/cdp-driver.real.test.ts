// Run: pnpm --dir apps/desktop exec tsx --test mcp/cdp-driver.real.test.ts
// The CDP driver against a REAL Chromium DevTools endpoint: real target
// discovery, real socket framing, real Runtime/Page replies, real reconnects,
// real error pages and a real listener that never answers. No test doubles;
// the driver's private counters are read, never replaced.
// Electron's window speaks the same protocol, so this is the endpoint the
// driver meets in `just electron-dev` minus the shell around it.
//
// Browser: CDP_TEST_BROWSER (a Chromium-family browser binary: Chromium, Chrome, Edge;
// not a raw Electron binary, which wants an app path, not a URL), else a
// Playwright Chromium under PLAYWRIGHT_BROWSERS_PATH or the usual install dirs,
// else Google Chrome on macOS. None found fails loudly, never skips.
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { connect, createServer as createNetServer, type AddressInfo, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { Cdp, CdpTimeoutError } from "./cdp-driver";
import { signalGroup, TaskkillUnavailableError } from "../electron/src/engine";
import { chromiumSandboxArgs } from "./chromium-sandbox";
import { findBrowser } from "./find-browser";
import { groupMembers, sweepGroup, type Member } from "./process-group";

async function freePort(): Promise<number> {
  const probe = createNetServer();
  await new Promise<void>((resolve) => probe.listen(0, "127.0.0.1", resolve));
  const { port } = probe.address() as AddressInfo;
  await new Promise<void>((resolve) => probe.close(() => resolve()));
  return port;
}

const SLOW_MS = 800;
let site: Server;
let origin = "";
let browser: ChildProcess;
let profile = "";
let cdpBase = "";

before(async () => {
  site = createServer((req, res) => {
    if (req.url === "/hang") {
      // Headers and half a page, then nothing: this document's load never fires.
      res.writeHead(200, { "content-type": "text/html" });
      res.write("<!doctype html><title>hang</title><p>");
      return;
    }
    if (req.url === "/slow.png") {
      // Holds the main document's load back well past the child frame's.
      setTimeout(() => {
        res.writeHead(204);
        res.end();
      }, SLOW_MS);
      return;
    }
    res.writeHead(200, { "content-type": "text/html" });
    if (req.url === "/framed") {
      res.end(
        `<!doctype html><title>framed</title><iframe src="/frame"></iframe><img src="/slow.png">`,
      );
      return;
    }
    const title =
      req.url === "/next" ? "next page" : req.url === "/frame" ? "frame" : "first page";
    res.end(`<!doctype html><title>${title}</title><p id="p">${title}</p>`);
  });
  await new Promise<void>((resolve) => site.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${(site.address() as AddressInfo).port}`;

  const port = await freePort();
  cdpBase = `http://127.0.0.1:${port}`;
  profile = mkdtempSync(join(tmpdir(), "cdp-real-"));
  const args = [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--no-first-run",
    "--no-default-browser-check",
    ...chromiumSandboxArgs(),
    `${origin}/`,
  ];
  // Its own process group on POSIX, so the teardown reaches renderer and GPU
  // helpers too; Windows has no groups and signalGroup uses taskkill /T.
  browser = spawn(findBrowser(), args, { stdio: "ignore", detached: process.platform !== "win32" });
  const deadline = Date.now() + 30_000;
  for (;;) {
    let response: Response | null = null;
    try {
      // Bounded per request (body included): a listener that accepts but
      // never answers must not outlast the 30 s deadline (Codex on #6830).
      response = await fetch(`${cdpBase}/json/list`, { signal: AbortSignal.timeout(5_000) });
    } catch {
      // not listening yet, or no answer within 5 s: only the request is retried
    }
    if (response) {
      if (!response.ok)
        throw new Error(`CDP ${cdpBase}/json/list answered ${response.status}`);
      // A reply that is not the target list fails here, not as a timeout.
      const list = (await response.json()) as { url: string; title: string }[];
      // Listed is not loaded: wait for the served title, not just the URL.
      if (
        list.some((t) => t.url.startsWith(origin) && t.title === "first page")
      )
        break;
    }
    if (browser.exitCode !== null || browser.signalCode !== null)
      throw new Error(
        `browser exited with ${browser.exitCode ?? browser.signalCode} before CDP answered`,
      );
    if (Date.now() > deadline)
      throw new Error(`CDP on ${cdpBase} did not list ${origin} within 30 s`);
    await new Promise((r) => setTimeout(r, 100));
  }
});

after(async () => {
  // Stop the whole browser tree, not just its leader, and wait for the leader's
  // exit (bounded) so no helper still holds the profile when it is removed
  // (Windows refuses that; Codex on #6830).
  const pid = browser?.pid;
  try {
    if (pid !== undefined && process.platform === "win32" && (browser.exitCode !== null || browser.signalCode !== null))
      // taskkill /T walks only from a live leader and Windows has no group to sweep,
      // so a surviving helper cannot be found: fail rather than call this clean (Codex on #6830).
      throw new Error(`browser ${pid} exited before teardown; a helper may still hold ${profile} or the CDP port`);
    // Taken while the browser is alive, so the sweep can prove the group is still its
    // own after it exits (Codex on #6830).
    // A failed listing must not stop the cleanup below; the sweep then proves nothing.
    let remembered: Member[] = [];
    if (pid !== undefined && process.platform !== "win32" && browser.exitCode === null && browser.signalCode === null) {
      try {
        remembered = groupMembers(pid);
      } catch (error) {
        console.error(`could not list browser ${pid}'s group before stopping it: ${(error as Error).message}`);
      }
    }
    if (pid !== undefined && browser.exitCode === null && browser.signalCode === null) {
      const exited = new Promise<boolean>((r) => browser.once("exit", () => r(true)));
      const within = (ms: number) =>
        Promise.race([exited, new Promise<boolean>((r) => setTimeout(() => r(false), ms).unref())]);
      // On Windows a false means taskkill /T never reached the tree: force it, and fail
      // if even that cannot, since nothing else finds the helpers there (Codex on #6830).
      // No taskkill.exe to ask: kill the browser itself so the test leaves nothing
      // behind it can reach, then fail, since its helpers may survive (Codex on #6830).
      const signalTree = (signal: NodeJS.Signals): boolean => {
        try {
          return signalGroup(pid, signal);
        } catch (error) {
          if (error instanceof TaskkillUnavailableError) browser.kill("SIGKILL");
          throw error;
        }
      };
      const asked = signalTree("SIGTERM") || process.platform !== "win32";
      if (!asked || !(await within(10_000))) {
        if (!signalTree("SIGKILL") && process.platform === "win32") {
          // The browser itself is still ours to stop; its helpers cannot be proven gone.
          browser.kill("SIGKILL");
          await within(10_000);
          throw new Error(`taskkill /T /F could not stop browser ${pid}'s tree`);
        }
        if (!(await within(10_000)))
          throw new Error(`browser ${pid} survived SIGKILL for 10 s`);
      }
    }
    // A helper can outlive its leader: sweep what is left of the group, but only
    // while a remembered member proves its id was never reused (Codex on #6830).
    if (pid !== undefined && process.platform !== "win32") {
      const sweep = await sweepGroup(pid, remembered);
      if ("survived" in sweep)
        throw new Error(`browser ${pid}'s group still has ${sweep.survived.join(", ")} after SIGKILL`);
      if ("leftAlone" in sweep)
        throw new Error(
          `group ${pid} still has process(es) ${sweep.leftAlone.join(", ")} this run cannot prove are the browser's; left alone`,
        );
    }
  } finally {
    // Release every handle even when the stop failed, so the runner reports the
    // failure and exits instead of waiting on them (Codex on #6830).
    site?.closeAllConnections();
    site?.close();
    browser?.unref();
  }
  // Only once the tree is gone: a failed stop leaves the temp profile in place.
  if (profile)
    rmSync(profile, {
      recursive: true,
      force: true,
      maxRetries: 5,
      retryDelay: 200,
    });
});

test("execute returns real values, undefined as null, and passes arguments", async () => {
  const cdp = new Cdp(cdpBase, origin);
  try {
    assert.equal(
      await cdp.execute<string>("return document.title;"),
      "first page",
    );
    assert.equal(await cdp.execute<unknown>("return undefined;"), null);
    // Control: a present falsy value is not swallowed by the undefined mapping.
    assert.equal(await cdp.execute<number>("return 0;"), 0);
    assert.equal(
      await cdp.execute<number>("return arguments[0] + 1;", [41]),
      42,
    );
    await assert.rejects(
      cdp.execute("throw new Error('boom');"),
      /page script threw: .*boom/,
    );
  } finally {
    (cdp as unknown as { _drop(): void })._drop();
  }
});

test("navigate waits for the new page's load on a real browser", async () => {
  const cdp = new Cdp(cdpBase, origin);
  try {
    await cdp.navigate(`${origin}/next`);
    assert.equal(
      await cdp.execute<string>("return document.title;"),
      "next page",
    );
    const shot = Buffer.from(await cdp.screenshotBase64(), "base64");
    assert.equal(
      shot.subarray(1, 4).toString("ascii"),
      "PNG",
      "a real PNG frame",
    );
  } finally {
    (cdp as unknown as { _drop(): void })._drop();
  }
});

type Internals = {
  _drop(): void;
  _opened: number;
  _pending: Map<number, unknown>;
  _eventWaiters: Map<string, unknown[]>;
  _target: { id: string } | null;
};
const internals = (cdp: Cdp): Internals => cdp as unknown as Internals;
const armed = (cdp: Cdp): number =>
  [...internals(cdp)._eventWaiters.values()].reduce((n, w) => n + w.length, 0);
const until = async (check: () => boolean, what: string): Promise<void> => {
  const deadline = Date.now() + 10_000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error(`never happened: ${what}`);
    await new Promise((r) => setTimeout(r, 10));
  }
};
/** A loopback port nothing listens on: Chromium answers it with its error page. */
const refused = async (): Promise<string> => `http://127.0.0.1:${await freePort()}/`;

/**
 * Puts the browser's one page back on the first page over raw CDP. A driver cannot
 * always do it: a page left on an error page for some other port is listed under
 * that port's URL, which is not the app's, so no driver attaches to it.
 */
async function backToFirstPage(): Promise<void> {
  const list = (await (
    await fetch(`${cdpBase}/json/list`, { signal: AbortSignal.timeout(5_000) })
  ).json()) as { id: string; type: string; url: string; webSocketDebuggerUrl: string }[];
  const page = list.find((t) => t.type === "page");
  if (!page) throw new Error("the browser has no page left");
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.send(JSON.stringify({ id: 1, method: "Page.navigate", params: { url: `${origin}/` } }));
  const deadline = Date.now() + 10_000;
  try {
    for (;;) {
      const now = (await (
        await fetch(`${cdpBase}/json/list`, { signal: AbortSignal.timeout(5_000) })
      ).json()) as { id: string; title: string; url: string }[];
      const current = now.find((t) => t.id === page.id);
      if (current?.title === "first page" && current.url.startsWith(origin)) break;
      if (Date.now() > deadline) throw new Error("the page never got back to the first page");
      await new Promise((r) => setTimeout(r, 50));
    }
  } finally {
    socket.close();
  }
  // Closing the raw setup session can briefly make Chromium list the page with
  // an empty URL. Require two consecutive good lists before the driver under test.
  let stableLists = 0;
  for (;;) {
    const now = (await (
      await fetch(`${cdpBase}/json/list`, { signal: AbortSignal.timeout(5_000) })
    ).json()) as { id: string; title: string; url: string }[];
    const current = now.find((t) => t.id === page.id);
    if (current?.title === "first page" && current.url.startsWith(origin)) stableLists += 1;
    else stableLists = 0;
    if (stableLists >= 2) return;
    if (Date.now() > deadline) throw new Error("the page URL did not settle after setup");
    await new Promise((r) => setTimeout(r, 20));
  }
}

/** A driver on the first page, dropped afterwards whatever the test did. */
async function onFirstPage(body: (cdp: Cdp) => Promise<void>, timeoutMs?: number): Promise<void> {
  const cdp = new Cdp(cdpBase, origin, timeoutMs);
  try {
    await backToFirstPage();
    await body(cdp);
  } finally {
    internals(cdp)._drop();
  }
}

test("overlapping calls share one socket", async () => {
  await onFirstPage(async (cdp) => {
    const values = await Promise.all([1, 2, 3].map((n) => cdp.execute<number>(`return ${n};`)));
    assert.deepEqual(values, [1, 2, 3]);
    assert.equal(internals(cdp)._opened, 1, "if overlapping calls each open a socket then broken");
  });
});

/**
 * A byte-for-byte TCP relay in front of the real CDP port. Chromium builds each
 * webSocketDebuggerUrl from the Host header it is asked on, so a driver pointed
 * here keeps every socket on the relay. `hold` stops one connection's
 * browser-to-driver bytes (its close frame and FIN included) until `release`,
 * which is how a test makes a real close land late. Nothing is rewritten.
 */
async function startRelay(upstreamPort: number): Promise<{
  base: string;
  /** The open DevTools WebSocket connections (not the HTTP discovery ones). */
  webSockets: () => Socket[];
  listRequests: () => number;
  hold: (client: Socket) => void;
  release: (client: Socket) => void;
  close: () => void;
}> {
  type Pair = {
    client: Socket;
    upstream: Socket;
    ws: boolean;
    held: (string | Buffer)[] | null;
    ended: boolean;
  };
  const pairs: Pair[] = [];
  let listRequests = 0;
  const server = createNetServer((client) => {
    const upstream = connect(upstreamPort, "127.0.0.1");
    const pair: Pair = { client, upstream, ws: false, held: null, ended: false };
    pairs.push(pair);
    client.on("data", (chunk) => {
      // A DevTools socket opens with its upgrade request; discovery fetches do not.
      const request = chunk.toString("latin1");
      if (!pair.ws && request.startsWith("GET /devtools/")) pair.ws = true;
      listRequests += request.match(/^GET \/json\/list(?:[ ?]|$)/gm)?.length ?? 0;
      upstream.write(chunk);
    });
    client.on("end", () => upstream.end());
    upstream.on("data", (chunk) => (pair.held ? pair.held.push(chunk) : client.write(chunk)));
    upstream.on("end", () => (pair.held ? (pair.ended = true) : client.end()));
    for (const side of [client, upstream]) side.on("error", () => undefined);
    client.on("close", () => upstream.destroy());
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const find = (client: Socket) => pairs.find((p) => p.client === client);
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const deadline = Date.now() + 5_000;
  for (;;) {
    const list = (await (
      await fetch(`${base}/json/list`, {
        headers: { connection: "close" },
        signal: AbortSignal.timeout(1_000),
      })
    ).json()) as { type: string; url: string }[];
    if (list.some((target) => target.type === "page" && target.url.startsWith(origin))) break;
    if (Date.now() >= deadline) throw new Error("the relay never listed the app page URL");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  listRequests = 0;
  return {
    base,
    webSockets: () => pairs.filter((p) => p.ws && !p.client.destroyed).map((p) => p.client),
    listRequests: () => listRequests,
    hold: (client) => {
      const pair = find(client);
      if (pair) pair.held = [];
    },
    release: (client) => {
      const pair = find(client);
      if (!pair?.held) return;
      for (const chunk of pair.held) client.write(chunk);
      pair.held = null;
      if (pair.ended) client.end();
    },
    close: () => {
      for (const p of pairs) {
        p.client.destroy();
        p.upstream.destroy();
      }
      server.close();
    },
  };
}

test("an old socket's late close does not fail a call sent on its replacement", async () => {
  await onFirstPage(async () => {
    const relay = await startRelay(Number(new URL(cdpBase).port));
    const cdp = new Cdp(relay.base, origin);
    try {
      let firstFailure: Error | null = null;
      const first = cdp
        .execute("await new Promise((r) => setTimeout(r, 5000)); return 1;")
        .catch((error: Error) => {
          firstFailure = error;
          return error;
        });
      await until(
        () => internals(cdp)._pending.size === 1 || firstFailure !== null,
        "the first call is in flight",
      );
      assert.equal(firstFailure, null, `the first call failed before it was sent: ${String(firstFailure)}`);
      const live = relay.webSockets();
      assert.equal(live.length, 1, "one DevTools socket through the relay");
      for (const socket of live) relay.hold(socket); // the old socket's close is held back
      internals(cdp)._drop(); // what a capture timeout does
      const second = cdp.execute<number>("await new Promise((r) => setTimeout(r, 1000)); return 2;");
      await until(() => internals(cdp)._pending.size === 2, "the retry is in flight on the new socket");
      for (const socket of live) relay.release(socket); // ...and lands now
      // Control: the call that really was on the closed socket fails loudly, so the
      // late close did reach the driver while the retry was still waiting.
      assert.match(String(await first), /socket closed/);
      assert.equal(internals(cdp)._pending.size, 1, "the retry is still pending after the late close");
      assert.equal(await second, 2);
      assert.equal(internals(cdp)._opened, 2);
    } finally {
      internals(cdp)._drop();
      relay.close();
    }
  });
});

test("after a reset, reconnect reuses the cached target without rediscovery", async () => {
  await onFirstPage(async () => {
    const relay = await startRelay(Number(new URL(cdpBase).port));
    const cdp = new Cdp(relay.base, origin);
    try {
      assert.equal(await cdp.execute<number>("return 1;"), 1);
      const targetId = internals(cdp)._target?.id;
      const listed = relay.listRequests();
      assert.ok(targetId, "the first connection did not cache its target");
      assert.equal(listed, 1, "the first connection must discover one target");

      internals(cdp)._drop(); // what a capture timeout does
      assert.equal(await cdp.execute<number>("return 2;"), 2);
      assert.equal(internals(cdp)._opened, 2);
      assert.equal(internals(cdp)._target?.id, targetId);
      assert.equal(relay.listRequests(), listed, "the reconnect must not pick a target again");
    } finally {
      internals(cdp)._drop();
      relay.close();
    }
  });
});

test("navigate resolves on its own document's load, not a child frame's or DOMContentLoaded", async () => {
  await onFirstPage(async (cdp) => {
    const started = Date.now();
    await cdp.navigate(`${origin}/framed`);
    const elapsed = Date.now() - started;
    assert.equal(await cdp.execute<string>("return document.readyState;"), "complete");
    assert.equal(await cdp.execute<boolean>("return document.images[0].complete;"), true);
    // The frame and DOMContentLoaded finish long before the held image.
    assert.ok(elapsed >= SLOW_MS - 50, `navigate returned after ${elapsed} ms, before the page loaded`);
    assert.equal(armed(cdp), 0);
  });
});

test("navigate throws on a failed load and returns at once on a same-document change", async () => {
  await onFirstPage(async (cdp) => {
    await assert.rejects(cdp.navigate(await refused()), /failed: net::ERR_CONNECTION_REFUSED/);
    assert.equal(armed(cdp), 0, "if a failed navigate leaves its waiter armed then broken");
    await cdp.navigate(`${origin}/`);
    await cdp.navigate(`${origin}/#x`);
    assert.equal(await cdp.execute<string>("return location.hash;"), "#x");
    assert.equal(armed(cdp), 0, "if a same-document navigate leaves its waiter armed then broken");
  });
});

test("a navigate whose load never fires fails loudly, not silently", async () => {
  await onFirstPage(async (cdp) => {
    await assert.rejects(cdp.navigate(`${origin}/hang`), /\(loader \w+\) not seen within 1000 ms/);
    assert.equal(armed(cdp), 0);
  }, 1_000);
});

test("status reads the live URL, not the one cached at connect", async () => {
  await onFirstPage(async (cdp) => {
    assert.equal(((await cdp.status()) as { ready: boolean }).ready, true);
    // Another origin on the same server: localhost is not 127.0.0.1.
    const elsewhere = `${origin.replace("127.0.0.1", "localhost")}/next`;
    await cdp.execute(`location.href = ${JSON.stringify(elsewhere)};`);
    let after = { ready: true, target: { url: "" } };
    for (let i = 0; i < 100 && after.target.url !== elsewhere; i++) {
      after = (await cdp.status()) as typeof after;
      await new Promise((r) => setTimeout(r, 50));
    }
    try {
      assert.equal(after.target.url, elsewhere);
      assert.equal(after.ready, false, "if a stale cached app URL reports ready then broken");
    } finally {
      // Only this session can bring it back: a fresh driver finds no app page to attach to.
      await cdp.navigate(`${origin}/`);
    }
  });
});

test("a window on an error page keeps its session so a navigate can bring it back", async () => {
  await onFirstPage(async (cdp) => {
    await assert.rejects(cdp.navigate(await refused()));
    const stranded = (await cdp.status()) as { ready: boolean; target: { url: string } };
    assert.equal(stranded.ready, false);
    assert.match(stranded.target.url, /^chrome-error:/);
    await cdp.navigate(`${origin}/`);
    assert.equal(((await cdp.status()) as { ready: boolean }).ready, true);
    assert.equal(internals(cdp)._opened, 1, "if the session on the window was dropped then broken");
  });
});

test("after a reset, a window sent off the app origin is reattached so a navigate can bring it home", async () => {
  await onFirstPage(async (cdp) => {
    // Another origin on the same server: localhost is not 127.0.0.1.
    const elsewhere = `${origin.replace("127.0.0.1", "localhost")}/next`;
    await cdp.navigate(elsewhere);
    assert.equal(((await cdp.status()) as { ready: boolean }).ready, false);
    internals(cdp)._drop(); // what a capture timeout does (Codex and Sol on #6830)
    await cdp.navigate(`${origin}/`);
    assert.equal(((await cdp.status()) as { ready: boolean }).ready, true);
    assert.equal(internals(cdp)._opened, 2, "if no fresh session reattached the known window then broken");
  });
});

test("control: a different app page still takes the session over", async () => {
  await onFirstPage(async (cdp) => {
    await assert.rejects(cdp.navigate(await refused()));
    const before = (await cdp.status()) as { target: { id: string } };
    const opened = (await (
      await fetch(`${cdpBase}/json/new?${encodeURIComponent(`${origin}/next`)}`, {
        method: "PUT",
        signal: AbortSignal.timeout(5_000),
      })
    ).json()) as { id: string };
    try {
      let now = { ready: false, target: { id: before.target.id } };
      for (let i = 0; i < 100 && !now.ready; i++) {
        now = (await cdp.status()) as typeof now;
        if (!now.ready) await new Promise((r) => setTimeout(r, 50));
      }
      assert.equal(now.ready, true);
      assert.equal(now.target.id, opened.id, "the driver must follow the app page, not cling on");
      assert.equal(internals(cdp)._opened, 2);
    } finally {
      await fetch(`${cdpBase}/json/close/${opened.id}`, { signal: AbortSignal.timeout(5_000) });
    }
  });
});

test("a CDP endpoint that never answers fails loudly instead of hanging", async () => {
  // A real listener that accepts the connection and never writes a byte.
  const held: Socket[] = [];
  const silent = createNetServer((socket) => held.push(socket));
  await new Promise<void>((resolve) => silent.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${(silent.address() as AddressInfo).port}`;
  let hang: ReturnType<typeof setTimeout> | undefined;
  let captureHang: ReturnType<typeof setTimeout> | undefined;
  try {
    const failed = await Promise.race([
      new Cdp(base, origin, 300).status().catch((e: Error) => e),
      new Promise<"hung">((resolve) => (hang = setTimeout(() => resolve("hung"), 5_000))),
    ]);
    assert.notEqual(failed, "hung", "if a never-answering endpoint hangs the call then broken");
    assert.match(String(failed), /did not answer within 300 ms/);
    const started = Date.now();
    const captureFailed = await Promise.race([
      new Cdp(base, origin, 30_000, 500).screenshotBase64().catch((e: Error) => e),
      new Promise<"hung">((resolve) => (captureHang = setTimeout(() => resolve("hung"), 3_000))),
    ]);
    const elapsed = Date.now() - started;
    assert.notEqual(captureFailed, "hung", "if screenshot discovery uses the call timeout then broken");
    assert.ok(captureFailed instanceof CdpTimeoutError, `expected a timeout, got ${String(captureFailed)}`);
    assert.match(String(captureFailed), /did not answer within \d+ ms.*take the screenshot again/);
    assert.ok(elapsed < 2_000, `the 500 ms screenshot budget took ${elapsed} ms`);
    // Control: the request reached the listener, so the failure is its silence, not a refusal.
    assert.ok(held.length >= 1, "the silent listener was never reached");
  } finally {
    clearTimeout(hang);
    clearTimeout(captureHang);
    for (const socket of held) socket.destroy();
    silent.close();
  }
});

test("a capture that times out fails loudly and resets the session, never retakes silently", async () => {
  await onFirstPage(async () => {
    // A 1 ms budget: no real capture answers that fast, so this is a genuine timeout.
    const stalled = new Cdp(cdpBase, origin, undefined, 1);
    try {
      await stalled.execute("return 1;");
      assert.equal(internals(stalled)._opened, 1);
      const failed = await stalled.screenshotBase64().then(
        () => null,
        (e: unknown) => e,
      );
      assert.ok(failed instanceof CdpTimeoutError, `expected a timeout, got ${String(failed)}`);
      assert.match(String(failed), /timed out after 1 ms.*take the screenshot again/);
      assert.equal(internals(stalled)._opened, 1, "if the timeout was retaken silently then broken");
      // The stuck socket was dropped: the next call attaches afresh.
      assert.equal(await stalled.execute<number>("return 2;"), 2);
      assert.equal(internals(stalled)._opened, 2);
    } finally {
      internals(stalled)._drop();
    }
  });
});

test("control: a page script error is not mistaken for a timeout", async () => {
  await onFirstPage(async (cdp) => {
    const failed = await cdp.execute("throw new Error('timed out on purpose');").then(
      () => null,
      (e: unknown) => e,
    );
    assert.ok(failed instanceof Error);
    assert.ok(!(failed instanceof CdpTimeoutError), "if a message saying timed out reads as a timeout then broken");
  });
});
