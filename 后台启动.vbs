Option Explicit
Dim fileSystem, projectDirectory, launcherPath, command, shell
Set fileSystem = CreateObject("Scripting.FileSystemObject")
projectDirectory = fileSystem.GetParentFolderName(WScript.ScriptFullName)
launcherPath = fileSystem.BuildPath(projectDirectory, "launcher.py")
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = projectDirectory
' Use the system launcher so a moved/broken .venv can repair itself.
command = "py -3.12 " & Chr(34) & launcherPath & Chr(34) & " --background"
shell.Run command, 0, False
