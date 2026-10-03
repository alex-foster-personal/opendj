Library table: `table.library` with sticky `thead`, hover rows, `.star` / `.star.filled` ratings, `.chip` tags.

```html
<table class="library">
  <thead><tr><th>Title</th><th>Artist</th><th title="Tempo in BPM">BPM</th><th>Rating</th></tr></thead>
  <tbody>
    <tr><td>Sunday Clay</td><td>Hollis Wren</td><td title="Tempo 122.00 BPM">122.00</td>
        <td><span class="star filled">★</span><span class="star">★</span></td></tr>
  </tbody>
</table>
```

App chrome scale (0.9 rem). For the performance browser use the `.perf-root` row-state classes instead (TrackRowStates).
