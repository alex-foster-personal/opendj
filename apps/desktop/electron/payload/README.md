# payload

Empty placeholder for the electron-builder `extraResources` entry.

A packaged build reads this directory from `Contents/Resources/payload`
(`process.resourcesPath` joined with `payload`). The engine, Python runtime
and probe binaries are copied here by a later payload step. This directory is
not a payload build: it ships only this note so the resource path exists.
