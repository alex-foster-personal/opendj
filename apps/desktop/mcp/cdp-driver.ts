/**
 * cdp-driver: the Chrome DevTools Protocol seam for webview-mcp, so the same
 * tools drive the Electron shell that the WebDriver seam drives in Tauri.
 *
 * The Electron shell opens a CDP port only in unpackaged builds
 * (`OPENDJ_REMOTE_DEBUGGING_PORT`, apps/desktop/electron/src/main.ts), and
 * packaged builds have the inspect fuses off, so this adds no release surface.
 *
 * It speaks raw CDP over Node's built-in WebSocket: no Playwright, no
 * chrome-remote-interface, nothing new to install.
 *
 * Requirements (mini-PRD):
 *   ✔︎ the same four calls the WebDriver seam offers: execute (a function
 *     BODY with `arguments`, exactly WebDriver's execute/sync contract),
 *     navigate, screenshotBase64, status
 *   ✔︎ attaches to the app's page, never to DevTools or a worker target
 *   ✔︎ survives an Electron relaunch (the dev loop restarts the shell when its
 *     own TypeScript changes): a closed socket is reopened on the next call
 *   ✔︎ status reports ready only once the window is on the engine origin,
 *     not on the opendj:// bootstrap page it shows first
 *   [if] no CDP endpoint answers [then ⛔️] the call fails naming the env var
 *   [if] the page script throws [then ⛔️] the call fails with the page's error,
 *        never a silent undefined
 */

export interface PageDriver {
  execute<T>(script: string, args?: unknown[]): Promise<T>;
  navigate(url: string): Promise<void>;
  screenshotBase64(): Promise<string>;
  status(): Promise<unknown>;
}

interface CdpTarget {
  id: string;
  type: string;
  url: string;
  title: string;
  webSocketDebuggerUrl?: string;
}

export interface CdpReply {
  id?: number;
  result?: Record<string, unknown>;
  error?: { message: string };
}

/** A successful CDP reply's result, or why the reply carries none.

The protocol answers every successful call with a result object, `{}` when there
is nothing to say. A reply without one is malformed, and reading it as `{}`
would pass for a valid answer: a Page.navigate reply with no loaderId reads as a
same-document navigation and returns at once.
*/
export function replyResult(method: string, reply: CdpReply): Record<string, unknown> {
  const result = reply.result;
  if (typeof result !== "object" || result === null)
    throw new Error(`CDP ${method} replied with no result object`);
  return result;
}

const CALL_TIMEOUT_MS = 30_000;
/**
 * A capture issued while the window swaps renderers (the opendj:// bootstrap
 * handing over to the engine origin) never answers. Measured under Xvfb, Wed
 * 7 Oct 2026: the first capture after a relaunch hung the full 30 s while the
 * next one took 165 ms. So a screenshot, including connection setup, gets a short
 * budget, and a timeout drops the stuck socket and fails loudly; the caller's next
 * capture reconnects.
 */
const SCREENSHOT_TIMEOUT_MS = 5_000;

/** A CDP call that got no reply within its budget (never a protocol error). */
export class CdpTimeoutError extends Error {}

/**
 * The app's own page: one on the app origin (see `appPageProblem`), else the
 * bundled opendj:// bootstrap while it boots, else the target this session
 * already drove (`knownId`), else the one window left on Chromium's error page;
 * never devtools:// and never another http page such as a popup, which would
 * leave every tool acting on the wrong window. Two error pages are ambiguous,
 * so neither is picked: nothing proves which one is the app (Sol P1 on #6830).
 */
export function pickAppTarget(
  targets: readonly CdpTarget[],
  appOrigin: string,
  knownId: string | null = null,
): CdpTarget | null {
  const pages = targets.filter(
    (t) => t.type === "page" && !!t.webSocketDebuggerUrl,
  );
  // A window that loaded nothing (Vite was down) sits on Chromium's error
  // page; attaching to it is what lets `navigate` bring the window back.
  const errorPages = pages.filter((t) => t.url.startsWith("chrome-error:"));
  return (
    pages.find((t) => appPageProblem(t.url, appOrigin) === null) ??
    pages.find((t) => t.url.startsWith("opendj:")) ??
    (knownId === null ? undefined : pages.find((t) => t.id === knownId)) ??
    (errorPages.length === 1 ? errorPages[0] : null)
  );
}

/**
 * Why a Page.navigate reply cannot be trusted, or null. A failed load is a
 * successful reply carrying errorText, not a protocol error. CDP requires a
 * frameId on every reply, including a same-document one with no loaderId, so a
 * reply without one is a contract failure, never a hash change that "succeeded"
 * (Sol P1 on #6830).
 */
export function navigateReplyProblem(reply: Record<string, unknown>): string | null {
  if (typeof reply.errorText === "string" && reply.errorText !== "")
    return `failed: ${reply.errorText}`;
  if (typeof reply.frameId !== "string" || reply.frameId === "")
    return `got a Page.navigate reply with no frameId: ${JSON.stringify(reply)}`;
  if (reply.loaderId !== undefined && (typeof reply.loaderId !== "string" || reply.loaderId === ""))
    return `got a Page.navigate reply with a malformed loaderId: ${JSON.stringify(reply)}`;
  return null;
}

/** WebDriver's execute/sync body, as one CDP expression that resolves to its value. */
export function wrapScript(script: string, args: readonly unknown[]): string {
  return `(async function () { ${script}\n}).apply(null, ${JSON.stringify(args)})`;
}

/** The value a Runtime.evaluate reply (returnByValue) carries, or why it carries none.

A script that returns undefined comes back as a remote object of type
"undefined" with no value; WebDriver answers null there, so this does too
(eval_js stringifies it). Anything else without a value (NaN, a BigInt, a
malformed reply) is refused rather than read as undefined.
*/
export function evaluationValue(result: unknown): unknown {
  if (typeof result !== "object" || result === null)
    throw new Error("Runtime.evaluate replied with no result object");
  const remote = result as {
    type?: unknown;
    value?: unknown;
    unserializableValue?: unknown;
  };
  if ("value" in remote) return remote.value;
  if (remote.type === "undefined") return null;
  if (remote.unserializableValue !== undefined)
    throw new Error(
      `page script returned ${String(remote.unserializableValue)}, which JSON cannot carry`,
    );
  throw new Error(
    `Runtime.evaluate returned a ${String(remote.type)} with no value`,
  );
}

/**
 * Which Page.lifecycleEvent ends one navigate. Only the 'load' of THIS
 * navigation's frame and loader counts: Page.loadEventFired carries neither, and
 * a real Chromium sends the previous page's load, a child frame's load and
 * DOMContentLoaded around the one we want (fixtures/cdp-lifecycle-iframe-navigate.json).
 * Events seen before the Page.navigate reply names the loader are kept and
 * checked once it does, so an early load of ours still counts.
 */
export class LoadWait {
  private _want: { frameId: unknown; loaderId: unknown } | null = null;
  private readonly _early: Array<Record<string, unknown>> = [];

  /** One lifecycle event: true once it is this navigation's own load. */
  see(params: Record<string, unknown>): boolean {
    if (this._want === null) {
      this._early.push(params);
      return false;
    }
    return this._ours(params);
  }

  /** The reply named the frame and loader: true if their load already arrived. */
  expect(frameId: unknown, loaderId: unknown): boolean {
    this._want = { frameId, loaderId };
    return this._early.some((p) => this._ours(p));
  }

  private _ours(p: Record<string, unknown>): boolean {
    return (
      this._want !== null &&
      p.name === "load" &&
      p.frameId === this._want.frameId &&
      p.loaderId === this._want.loaderId
    );
  }
}

/** Why `href` is not the app page an agent wants to drive, or null when it is. */
export function appPageProblem(
  href: string,
  appOrigin: string,
): string | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return `the page URL ${JSON.stringify(href)} does not parse`;
  }
  if (url.protocol === "opendj:")
    return "the shell is still on its bootstrap page; call status again";
  // Exact origin only: any other loopback page (a popup, another local
  // server) must never be reported as the app.
  return url.origin === new URL(appOrigin).origin
    ? null
    : `the page is on ${url.origin}, not the app at ${appOrigin}`;
}

export class Cdp implements PageDriver {
  private _socket: WebSocket | null = null;
  private _target: CdpTarget | null = null;
  private _nextId = 0;
  /** Sockets opened so far: overlapping calls must share one (read by the real-browser tests). */
  private _opened = 0;
  /** In-flight calls, each tied to the socket it was sent on (see the close handler). */
  private readonly _pending = new Map<
    number,
    { socket: WebSocket; settle: (reply: CdpReply) => void }
  >();
  /** One connect at a time: overlapping tool calls share it instead of each opening a socket. */
  private _connecting: Promise<WebSocket> | null = null;
  /** Waiters for protocol events (no id), by method name. */
  private readonly _eventWaiters = new Map<
    string,
    Array<(params: Record<string, unknown>) => void>
  >();

  /** @param _appOrigin the page origin the app must be on: MDT_WEBVIEW_MCP_APP,
   * else the engine origin the packed shell loads its pages from.
   * @param _timeoutMs bound on every CDP call and discovery fetch (tests shorten it).
   * @param _screenshotTimeoutMs bound on one screenshot including setup (tests shorten it). */
  constructor(
    private readonly _base: string,
    private readonly _appOrigin: string,
    private readonly _timeoutMs: number = CALL_TIMEOUT_MS,
    private readonly _screenshotTimeoutMs: number = SCREENSHOT_TIMEOUT_MS,
  ) {}

  async execute<T>(script: string, args: unknown[] = []): Promise<T> {
    const reply = await this._send("Runtime.evaluate", {
      expression: wrapScript(script, args),
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    const details = reply.exceptionDetails as
      | { text?: string; exception?: { description?: string } }
      | undefined;
    if (details) {
      throw new Error(
        `page script threw: ${details.exception?.description ?? details.text ?? "unknown error"}`,
      );
    }
    return evaluationValue(reply.result) as T;
  }

  async navigate(url: string): Promise<void> {
    // Page.navigate answers when navigation STARTS; WebDriver's /url waits for
    // load, so wait for the load event to match it (a Vite route can take seconds).
    await this._send("Page.enable", {});
    await this._send("Page.setLifecycleEventsEnabled", { enabled: true });
    // Armed before the navigate is sent so a fast load cannot slip past it.
    const wait = new LoadWait();
    const load = this._waitForEvent("Page.lifecycleEvent", this._timeoutMs, (p) => wait.see(p));
    let reply: Record<string, unknown>;
    try {
      reply = await this._send("Page.navigate", { url });
    } catch (error) {
      load.cancel();
      throw error;
    }
    const problem = navigateReplyProblem(reply);
    if (problem !== null) {
      load.cancel();
      throw new Error(`navigate to ${url} ${problem}`);
    }
    // No loaderId means a same-document navigation (a hash change): no load event follows.
    if (reply.loaderId === undefined) {
      load.cancel();
      return;
    }
    if (wait.expect(reply.frameId, reply.loaderId)) {
      load.cancel();
      return;
    }
    if ((await load.seen) === "timeout") {
      throw new Error(
        `navigate to ${url}: its load event (loader ${String(reply.loaderId)}) not seen within ${this._timeoutMs} ms`,
      );
    }
  }

  /** Waits for one protocol event that `match` accepts (any, by default).
   * `seen` never rejects: it settles 'event', 'timeout' or 'cancelled', so an
   * unawaited waiter cannot fail silently. */
  private _waitForEvent(
    method: string,
    timeoutMs: number,
    match: (params: Record<string, unknown>) => boolean = () => true,
  ): { seen: Promise<"event" | "timeout" | "cancelled">; cancel: () => void } {
    let finish: (how: "event" | "timeout" | "cancelled") => void = () =>
      undefined;
    const seen = new Promise<"event" | "timeout" | "cancelled">((resolve) => {
      const onEvent = (params: Record<string, unknown>): void => {
        if (match(params)) finish("event");
      };
      const timer = setTimeout(() => finish("timeout"), timeoutMs);
      finish = (how) => {
        clearTimeout(timer);
        const left = (this._eventWaiters.get(method) ?? []).filter(
          (w) => w !== onEvent,
        );
        if (left.length > 0) this._eventWaiters.set(method, left);
        else this._eventWaiters.delete(method);
        finish = () => undefined;
        resolve(how);
      };
      this._eventWaiters.set(method, [
        ...(this._eventWaiters.get(method) ?? []),
        onEvent,
      ]);
    });
    return { seen, cancel: () => finish("cancelled") };
  }

  async screenshotBase64(): Promise<string> {
    try {
      const reply = await this._send(
        "Page.captureScreenshot",
        { format: "png" },
        this._screenshotTimeoutMs,
      );
      return reply.data as string;
    } catch (error) {
      // No silent retake: a later frame could show a different page than the one
      // asked about (Sol on #6830). Drop the stuck socket so the next call reconnects,
      // and say so; any other error passes through untouched.
      if (!(error instanceof CdpTimeoutError)) throw error;
      this._drop();
      throw new CdpTimeoutError(
        `${error.message}; the window may have been swapping renderers (just after a ` +
          `relaunch). The CDP session was reset, so take the screenshot again.`,
      );
    }
  }

  async status(): Promise<unknown> {
    const version = await this._json<Record<string, string>>("/json/version");
    await this._ensure();
    // Read the live URL every call: the target URL cached at connect goes
    // stale after a navigate, an external link or a Chromium error page.
    const live = await this.execute<string>("return location.href;");
    if (this._target) this._target = { ...this._target, url: live };
    const why = appPageProblem(live, this._appOrigin);
    return {
      ready: why === null,
      ...(why === null ? {} : { waiting: why }),
      seam: "cdp",
      endpoint: this._base,
      browser: version.Browser,
      target: { id: this._target?.id, url: live },
    };
  }

  private async _json<T>(
    path: string,
    timeoutMs: number = this._timeoutMs,
  ): Promise<T> {
    if (timeoutMs <= 0)
      throw new CdpTimeoutError(
        `CDP ${path} at ${this._base} did not answer within ${timeoutMs} ms`,
      );
    let response: Response;
    try {
      // Bounded: an endpoint that accepts and never answers must fail, not hang the tool.
      response = await fetch(`${this._base}${path}`, {
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      if (error instanceof Error && error.name === "TimeoutError") {
        throw new CdpTimeoutError(
          `CDP ${path} at ${this._base} did not answer within ${timeoutMs} ms`,
        );
      }
      throw new Error(
        `no CDP endpoint at ${this._base} (${String(error)}). Is the unpackaged Electron shell ` +
          `running with OPENDJ_REMOTE_DEBUGGING_PORT? (just electron-dev sets it.) Packaged builds expose no CDP.`,
      );
    }
    if (!response.ok) throw new Error(`CDP ${path} -> HTTP ${response.status}`);
    return (await response.json()) as T;
  }

  private _drop(): void {
    const socket = this._socket;
    this._socket = null;
    socket?.close();
  }

  private _ensure(
    deadline: number = Date.now() + this._timeoutMs,
    timeoutMs: number = this._timeoutMs,
  ): Promise<WebSocket> {
    this._connecting ??= this._connect(deadline, timeoutMs).finally(() => {
      this._connecting = null;
    });
    return this._connecting;
  }

  private async _connect(deadline: number, timeoutMs: number): Promise<WebSocket> {
    if (this._socket && this._socket.readyState === WebSocket.OPEN) {
      if (
        this._target &&
        appPageProblem(this._target.url, this._appOrigin) === null
      )
        return this._socket;
      // Attached to the bootstrap page: re-read the list so the session
      // follows the window onto the engine origin once it gets there.
      const list = await this._json<CdpTarget[]>(
        "/json/list",
        this._remaining(deadline, timeoutMs, "connection setup"),
      );
      // No app page anywhere (the window is on an error page): stay on our
      // own target, so a navigate can still bring the window back.
      const fresh = pickAppTarget(
        list,
        this._appOrigin,
        this._target?.id ?? null,
      );
      if (fresh && fresh.id === this._target?.id) {
        this._target = fresh;
        return this._socket;
      }
      this._drop();
      if (fresh?.webSocketDebuggerUrl) {
        return this._open(
          fresh,
          this._remaining(deadline, timeoutMs, "connection setup"),
        );
      }
      throw new Error(
        `CDP at ${this._base} lists no app page on ${this._appOrigin} yet ` +
          `(the shell may still be booting its engine)`,
      );
    }
    if (this._target?.webSocketDebuggerUrl) {
      try {
        return await this._open(
          this._target,
          this._remaining(deadline, timeoutMs, "connection setup"),
        );
      } catch (error) {
        // A relaunch can invalidate the cached target. Discover once only after
        // that direct reconnect fails; capture calls keep the same deadline.
        this._target = null;
        if (Date.now() >= deadline) throw error;
      }
    }
    const list = await this._json<CdpTarget[]>(
      "/json/list",
      this._remaining(deadline, timeoutMs, "connection setup"),
    );
    // Direct reconnect above preserves the known target after a reset. Discovery
    // here is the fallback for first connect or a cached target invalidated by a
    // relaunch (Codex and Sol on #6830).
    const target = pickAppTarget(
      list,
      this._appOrigin,
      this._target?.id ?? null,
    );
    if (!target?.webSocketDebuggerUrl) {
      throw new Error(
        `CDP at ${this._base} lists no app page on ${this._appOrigin} yet ` +
          `(the shell may still be booting its engine)`,
      );
    }
    return this._open(
      target,
      this._remaining(deadline, timeoutMs, "connection setup"),
    );
  }

  private async _open(target: CdpTarget, timeoutMs: number): Promise<WebSocket> {
    if (!target.webSocketDebuggerUrl)
      throw new Error(`CDP target ${target.id} has no WebSocket debugger URL`);
    if (timeoutMs <= 0)
      throw new CdpTimeoutError(
        `CDP socket to ${target.url} (${this._base}) did not open within ${timeoutMs} ms`,
      );
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    this._opened += 1;
    await new Promise<void>((resolve, reject) => {
      // A socket stuck in CONNECTING never fires open or error; bound it.
      const timer = setTimeout(() => {
        socket.close();
        reject(
          new CdpTimeoutError(
            `CDP socket to ${target.url} (${this._base}) did not open within ${timeoutMs} ms`,
          ),
        );
      }, timeoutMs);
      socket.addEventListener(
        "open",
        () => {
          clearTimeout(timer);
          resolve();
        },
        { once: true },
      );
      socket.addEventListener(
        "error",
        () => {
          clearTimeout(timer);
          reject(new Error(`CDP socket to ${target.url} failed to open`));
        },
        { once: true },
      );
    });
    socket.addEventListener("message", (event) => {
      const reply = JSON.parse(String(event.data)) as CdpReply & {
        method?: string;
        params?: Record<string, unknown>;
      };
      if (reply.id === undefined) {
        // Each waiter removes itself once its match fires; copy so that is safe mid-loop.
        const waiters =
          reply.method === undefined
            ? undefined
            : this._eventWaiters.get(reply.method);
        for (const wake of [...(waiters ?? [])]) wake(reply.params ?? {});
        return;
      }
      this._pending.get(reply.id)?.settle(reply);
      this._pending.delete(reply.id);
    });
    socket.addEventListener("close", () => {
      if (this._socket === socket) this._socket = null;
      // Settle only this socket's calls: a retry already sent on the
      // replacement socket must survive the old one's late close event.
      for (const [id, call] of this._pending) {
        if (call.socket !== socket) continue;
        this._pending.delete(id);
        call.settle({
          id,
          error: { message: "CDP socket closed (shell relaunched?)" },
        });
      }
    });
    this._socket = socket;
    this._target = target;
    return socket;
  }

  private _remaining(deadline: number, timeoutMs: number, what: string): number {
    const remaining = deadline - Date.now();
    if (remaining <= 0)
      throw new CdpTimeoutError(`CDP ${what} timed out after ${timeoutMs} ms`);
    return remaining;
  }

  private async _send(
    method: string,
    params: Record<string, unknown>,
    timeoutMs: number = this._timeoutMs,
  ): Promise<Record<string, unknown>> {
    const deadline = Date.now() + timeoutMs;
    const socket = await this._ensure(deadline, timeoutMs);
    const remaining = this._remaining(deadline, timeoutMs, method);
    const id = ++this._nextId;
    let timedOut = false;
    const reply = await new Promise<CdpReply>((resolve) => {
      const timer = setTimeout(() => {
        this._pending.delete(id);
        timedOut = true;
        resolve({
          id,
          error: { message: `CDP ${method} timed out after ${timeoutMs} ms` },
        });
      }, remaining);
      this._pending.set(id, {
        socket,
        settle: (r) => {
          clearTimeout(timer);
          resolve(r);
        },
      });
      socket.send(JSON.stringify({ id, method, params }));
    });
    if (timedOut) throw new CdpTimeoutError(`CDP ${method}: ${reply.error?.message}`);
    if (reply.error) throw new Error(`CDP ${method}: ${reply.error.message}`);
    return replyResult(method, reply);
  }
}
