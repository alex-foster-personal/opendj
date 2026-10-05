// One bundle for DECKUX-21: the drop helper and the Space target share a single
// recent-deck module instance, as they do in the app.
export { applyDeckTrackDrop } from '$lib/rb/deck-track-drop';
export { getRecentDeck, noteRecentDeck } from '$lib/rb/recent-deck';
