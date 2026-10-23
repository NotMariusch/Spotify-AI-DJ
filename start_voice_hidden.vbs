Option Explicit

Dim shell, fso, baseDir, pythonPath, scriptPath, logPath, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonPath = baseDir & "\.venv\Scripts\python.exe"
scriptPath = baseDir & "\scripts\dj_voice.py"
logPath = baseDir & "\data\voice.log"

If Not fso.FileExists(pythonPath) Or Not fso.FileExists(scriptPath) Then
    MsgBox "Voice-Start fehlgeschlagen: .venv\Scripts\python.exe oder scripts\dj_voice.py fehlt in " & baseDir, 16, "Spotify DJ Voice"
    WScript.Quit 1
End If
If Not fso.FolderExists(baseDir & "\data") Then fso.CreateFolder(baseDir & "\data")
shell.CurrentDirectory = baseDir
shell.Environment("PROCESS")("PYTHONUTF8") = "1"
shell.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"
command = Chr(34) & pythonPath & Chr(34) & " -u " & Chr(34) & scriptPath & Chr(34) _
    & " >> " & Chr(34) & logPath & Chr(34) & " 2>&1"
shell.Run Chr(34) & shell.ExpandEnvironmentStrings("%ComSpec%") & Chr(34) _
    & " /d /s /c " & Chr(34) & command & Chr(34), 0, False
