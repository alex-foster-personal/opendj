' run_worker_hidden.vbs - keep the long-running Demucs worker attached to Task Scheduler, not a console.
' Calling card: [codex-bifrost2](codex://thread/019faf65-5136-7980-bdd5-cbe229c73b3d), 2026-07-29. Added the canonical no-focus Demucs task launcher.
Option Explicit

Dim shell, command, exitCode
Set shell = CreateObject("WScript.Shell")
command = shell.ExpandEnvironmentStrings("%ComSpec%") & " /d /c " & Chr(34) & Chr(34) & "D:\demucs-work\run_worker.cmd" & Chr(34) & Chr(34)
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
