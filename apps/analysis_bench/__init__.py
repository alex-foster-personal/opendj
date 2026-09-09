"""Lane-agnostic analysis benchmark harness (NATIVE-11, spec section 6 / D6).

Four lanes -- beatgrid, key, waveform, loudness -- each need the same three
things: a fixture set every host can prove it holds byte for byte, a scorer
whose version is stamped into every artifact so two rounds are comparable, and
a numbered round appended to one experiment log. Before this package each lane
was growing its own, which meant five rulers and no way to tell a rescoring
apart from a re-measurement.

The split is deliberate. `lanes` says what a lane IS (its log, its round floor,
its controls, its scorer). `bundles` owns fixture identity: sealing,
checksums and the bundle id. `stores` owns where a sealed bundle travels.
`rounds` owns the log. `scorers/` holds one module per lane, each exposing
SCORER_VERSION plus score_bundle/render_table, so `cli` never needs to know
which lane it is driving.
"""

from __future__ import annotations

__version__ = "1.0.0"
