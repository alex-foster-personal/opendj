import "@testing-library/jest-dom/vitest";

// jsdom ships no ResizeObserver, and cmdk constructs one on mount, so every
// test that renders <Command> threw `ResizeObserver is not defined` before a
// single assertion ran. That left the whole launcher component suite red on
// main, which is why nothing noticed the palette rendering invented tracks
// (issue #1542). This is a jsdom gap, not a behavior stand-in: the palette
// under test never reads a measurement from it.
if (!("ResizeObserver" in globalThis)) {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = ResizeObserverStub;
}

// Same class of jsdom gap: cmdk scrolls the selected item into view, and jsdom
// implements no scrolling at all. Without this, every test that renders a ROW
// (rather than just the empty state) died in a layout effect.
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoViewStub(): void {};
}
