Browser row states: `tr.rb-row-selected` (blue fill), `.rb-row-loaded` on title/artist cells (green), `tr.rb-row-master` with `.c-title` / `.c-artist` (gold, bold). Inside `.perf-root`.

```html
<tr class="rb-row-selected"><td class="c-title">Sunday Clay</td><td class="c-artist">Hollis Wren</td></tr>
<tr><td class="c-title rb-row-loaded">Open Windows</td><td class="c-artist rb-row-loaded">Cass Delaney</td></tr>
<tr class="rb-row-master"><td class="c-title rb-row-loaded">Daylight Hours</td><td class="c-artist rb-row-loaded">The Understudies</td></tr>
```

Rows are 11 px with 3 x 6 px cell padding and 1 px `--rb-border` dividers; states combine. Numeric cells carry a `title`
with the unit.
