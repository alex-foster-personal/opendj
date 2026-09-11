import assert from "node:assert/strict";
import { before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

let thread;
before(async () => {
  thread = await loadTypeScriptModule("src/lib/rb/feedback-pin-thread.ts");
});

const pin = (over = {}) => ({
  id: "abc123",
  text: "opening text",
  created_at: "2026-09-02T09:01:16Z",
  author: "operator",
  agent_note: null,
  updated_at: null,
  replies: null,
  ...over,
});

test("opening only", () => {
  const turns = thread.pinThread(pin());
  assert.equal(turns.length, 1);
  assert.equal(turns[0].kind, "opening");
  assert.equal(turns[0].text, "opening text");
});

test("legacy agent_note with no replies adds a synthetic agent turn", () => {
  const turns = thread.pinThread(
    pin({ agent_note: "queued as #1", updated_at: "2026-09-02T10:00:00Z" }),
  );
  assert.equal(turns.length, 2);
  assert.equal(turns[1].kind, "reply");
  assert.equal(turns[1].author, "agent");
  assert.equal(turns[1].text, "queued as #1");
  assert.equal(turns[1].created_at, "2026-09-02T10:00:00Z");
});

test("persisted replies do not duplicate agent_note", () => {
  const turns = thread.pinThread(
    pin({
      agent_note: "same note",
      replies: [
        {
          id: "r1",
          author: "agent",
          text: "same note",
          created_at: "2026-09-02T10:00:00Z",
        },
      ],
    }),
  );
  assert.equal(turns.length, 2);
  assert.equal(turns[1].text, "same note");
});

test("operator then agent then operator order is stable", () => {
  const turns = thread.pinThread(
    pin({
      replies: [
        {
          id: "r1",
          author: "operator",
          text: "human follow-up",
          created_at: "2026-09-02T10:01:00Z",
        },
        {
          id: "r2",
          author: "agent",
          text: "agent answer",
          created_at: "2026-09-02T10:02:00Z",
        },
        {
          id: "r3",
          author: "operator",
          text: "thanks",
          created_at: "2026-09-02T10:03:00Z",
        },
      ],
    }),
  );
  assert.equal(turns.map((t) => t.text).join(" | "), "opening text | human follow-up | agent answer | thanks");
});
