import assert from "node:assert/strict";
import { before, describe, it } from "node:test";

import { loadTypeScriptModule } from "./load-typescript.mjs";

let formatLoadLatency;

before(async () => {
  ({ formatLoadLatency } = await loadTypeScriptModule(
    "src/lib/rb/format-load-latency.ts",
  ));
});

describe("formatLoadLatency", () => {
  it("rounds seconds to one decimal", () => {
    assert.equal(formatLoadLatency(2289), "2.3s to load");
    assert.equal(formatLoadLatency(1000), "1.0s to load");
  });

  it("rounds sub-second to ~50ms steps", () => {
    assert.equal(formatLoadLatency(312), "~300ms to load");
    assert.equal(formatLoadLatency(20), "~50ms to load");
  });

  // Pin 0da471a39765 (the maintainer, Wed 2 Sep 2026): a bare "~600ms" in the deck
  // corner does not say what took 600ms. The unit alone is not a label.
  // [if] the readout renders a duration with no "to load" suffix [then in the
  // brief the number is ambiguous with every other timing on the screen]
  it("says what the duration measures", () => {
    assert.equal(formatLoadLatency(600), "~600ms to load");
    assert.ok(formatLoadLatency(600).endsWith(" to load"));
    assert.ok(formatLoadLatency(4200).endsWith(" to load"));
  });

  // The empty string is "nothing to say", not "0ms to load": suffixing it
  // would put a stray label in the deck corner on a deck that never loaded.
  it("keeps the not-measured case empty, with no suffix", () => {
    assert.equal(formatLoadLatency(Number.NaN), "");
    assert.equal(formatLoadLatency(-1), "");
  });
});
