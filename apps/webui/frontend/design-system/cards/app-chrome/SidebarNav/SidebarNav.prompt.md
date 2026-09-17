App shell: `.app-shell` grid (220 px `.sidebar` + `.content`), sidebar `h1` in accent, nav links with `.active`.

```html
<div class="app-shell">
  <aside class="sidebar">
    <h1>Open DJ</h1>
    <nav><a class="active">Library</a><a>Sets</a><a>Settings</a></nav>
  </aside>
  <main class="content">...</main>
</div>
```

Use for every non-performance page. The content column scrolls horizontally for wide tables instead of collapsing them.
