# Animations

One plain-ASCII animation per term that benefits from motion, written as frames in
`<slug>.txt`, frames separated by a line containing only `@@`. Each frame is at most 24 lines by
72 columns, plain ASCII (no color, no box-drawing characters). Players show one frame every
400 to 700 ms and loop. `build_index.py` attaches the frames to the term's entry in `index.json`
and fails on an oversized or non-ASCII frame.
