' PenAI-Launcher.vbs - Defender-safe alternative to .bat
' Double-click this instead of .bat - VBScript is AMSI-scanned but typically NOT blocked
' like PowerShell from USB. It launches start-agent.bat with visible window.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
' Try E:\START_HERE / E:\pen AI locations
If fso.FileExists(scriptDir & "\pen AI\agent\start-agent.bat") Then
  bat = scriptDir & "\pen AI\agent\start-agent.bat"
ElseIf fso.FileExists(scriptDir & "\agent\start-agent.bat") Then
  bat = scriptDir & "\agent\start-agent.bat"
Else
  bat = scriptDir & "\START_HERE.bat"
End If
Set sh = CreateObject("WScript.Shell")
' 1 = normal window, false = wait
sh.Run Chr(34) & bat & Chr(34), 1, False
