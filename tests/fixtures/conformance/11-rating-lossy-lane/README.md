# 11-rating-lossy-lane

Covers Appendix B item 11: "Track with no rating on Serato, 5-star
elsewhere -- ratings lossy-lane". A single 5-star track exercises the
asymmetric rating support across adapters: Serato (no default column)
drops the field under the 'rating' mask; Traktor maps 5 stars to its
255 ranking byte and round-trips losslessly. Rekordbox and djay will
join the lossless side when their adapters ship.
