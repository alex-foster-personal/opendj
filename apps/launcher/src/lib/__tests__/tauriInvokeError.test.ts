import { describe, expect, it } from "vitest";

import { tauriInvokeErrorMessage } from "../tauriInvokeError";

describe("tauriInvokeErrorMessage", () => {
  it("uses Error.message when err is an Error", () => {
    expect(tauriInvokeErrorMessage(new Error("db locked"))).toBe("db locked");
  });

  it("stringifies non-Error rejects", () => {
    expect(tauriInvokeErrorMessage("plain string")).toBe("plain string");
    expect(tauriInvokeErrorMessage(404)).toBe("404");
  });
});
