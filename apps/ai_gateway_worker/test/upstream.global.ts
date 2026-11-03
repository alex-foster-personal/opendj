// Node-side global setup: a real loopback HTTP server that replays recorded OpenAI-compatible
// SSE frames as the upstream. GET /seen returns every request it received, so tests assert on
// what the Worker actually forwarded rather than trusting it.
import { readFileSync } from "node:fs";
import { createServer, type IncomingHttpHeaders } from "node:http";
import type { AddressInfo } from "node:net";
import type { TestProject } from "vitest/node";

/** The fixture split after each blank line, separators kept, so a replay is byte-identical. */
const FRAMES = readFileSync(new URL("./fixtures/openrouter-stream.sse", import.meta.url), "utf8").split(/(?<=\n\r?\n)/);
const FRAMES_WITHOUT_USAGE = FRAMES.filter((f) => !f.includes('"usage"'));

export default async function setup(project: TestProject) {
  const seen: { headers: IncomingHttpHeaders; body: unknown }[] = [];
  const server = createServer(async (req, res) => {
    if (req.method === "GET" && req.url === "/seen") {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(seen));
      return;
    }
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw) as { model: string };
    seen.push({ headers: req.headers, body });
    if (body.model === "test/upstream-500") {
      res.writeHead(500, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { message: "provider down", code: 500 } }));
      return;
    }
    res.writeHead(200, { "content-type": "text/event-stream" });
    if (body.model === "test/disconnect") {
      res.write(FRAMES[0] + FRAMES[1]);
      await new Promise((r) => setTimeout(r, 20));
      res.socket!.destroy();
      return;
    }
    for (const frame of body.model === "test/no-usage" ? FRAMES_WITHOUT_USAGE : FRAMES) {
      res.write(frame);
      await new Promise((r) => setTimeout(r, 5));
    }
    res.end();
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  project.provide("upstreamUrl", `http://127.0.0.1:${(server.address() as AddressInfo).port}`);
  return () => new Promise<void>((resolve) => server.close(() => resolve()));
}
