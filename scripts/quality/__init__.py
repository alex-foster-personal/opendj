"""Stretch-quality measurement harness (lane agentB).

Binding spec: ``.planning/QUALITY-METHODOLOGY-RECONCILED.md``.

The package is deliberately numpy-only. Decoding happens in real Chromium
(``OfflineAudioContext.decodeAudioData``, the same decoder the deck uses), so
no soundfile/librosa/torch dependency is needed anywhere and nothing heavy
enters the repo venv.
"""
