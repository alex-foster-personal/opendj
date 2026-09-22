/**
 * Pin board badges and draw rules (issue #3782, FB-15).
 */

import assert from "node:assert/strict";
import { before, test } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

let board;
before(async () => {
  board = await loadTypeScriptModule("src/lib/rb/feedback-pin-board.ts");
});

const pin = (over = {}) => ({
  id: "abc123",
  x_pct: 50,
  y_pct: 50,
  anchor: "#deck-a",
  page: "/performance",
  text: "test",
  created_at: "2026-09-01T09:00:00Z",
  status: "open",
  environment: { viewport_width: 1000, viewport_height: 800 },
  ...over,
});

test("isPinDrawn hides only archived pins", () => {
  assert.equal(board.isPinDrawn(pin()), true);
  assert.equal(board.isPinDrawn(pin({ status: "harvested" })), true);
  assert.equal(board.isPinDrawn(pin({ status: "archived" })), false);
});

test("route-moved badge when page differs from pathname", () => {
  const state = board.pinBoardState(pin({ page: "/library" }), "/performance", false);
  assert.ok(state.badges.includes("route-moved"));
});

test("anchorMovedAtPin compares describeAnchor at pixel point", () => {
  const moved = board.anchorMovedAtPin(pin({ anchor: "#old" }), () => ({
    id: "new",
    tagName: "BUTTON",
  }));
  assert.equal(moved, true);
  const same = board.anchorMovedAtPin(pin({ anchor: "#deck-a" }), () => ({
    id: "deck-a",
    tagName: "DIV",
  }));
  assert.equal(same, false);
});

test("regressed badge when updated_at is after fixed_at", () => {
  const state = board.pinBoardState(
    pin({
      status: "open",
      fixed_in_sha: "abc1234",
      fixed_at: "2026-09-01T10:00:00.000000Z",
      updated_at: "2026-09-02T10:00:00.000000Z",
    }),
    "/performance",
    false
  );
  assert.ok(state.badges.includes("regressed"));
});
