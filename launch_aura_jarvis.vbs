Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
strDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = strDir
objShell.Run "pythonw -m src.aura.agents.aura_v4 --web --overlay --mode rw --workspace . --port 8000", 0, False
