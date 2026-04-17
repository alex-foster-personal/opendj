# Info.plist additions

Tauri v2 auto-generates Info.plist from `tauri.conf.json`. For v1 we need these
extras that Tauri doesn't yet wire directly through JSON. Inject via
`bundle.macOS.infoPlist` override when tauri-cli supports it, or patch the
generated `.plist` in a post-build step (tracked in `apps/launcher/README.md`
Open Questions).

```xml
<key>LSUIElement</key>
<true/>
<key>NSHighResolutionCapable</key>
<true/>
```

`LSUIElement=true` hides the Dock tile -- required by D5 (menu-bar-only + floating
palette). `NSHighResolutionCapable` is standard for Retina displays.
