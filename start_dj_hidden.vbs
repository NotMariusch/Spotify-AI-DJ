Option Explicit

Dim WshShell, fso, baseDir, lockFile, f, pid, wmi, procs

Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = baseDir

' Instance protection is handled atomically by the Python server and DJ.

If Not fso.FileExists(baseDir & "\start_dj.bat") Then
    MsgBox "Cannot find start_dj.bat in " & baseDir, 16, "Spotify DJ"
    WScript.Quit 1
End If

WshShell.Run Chr(34) & baseDir & "\start_dj.bat" & Chr(34), 0, False

Set WshShell = Nothing
Set fso = Nothing