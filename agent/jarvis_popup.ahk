#Requires AutoHotkey v2.0
#SingleInstance Force
#NoTrayIcon

; Jampandu popup. Press Ctrl+Shift+J to send selected text to the local model.
; User text is written to a temporary file; it is never interpolated into a
; command line.

global AGENT_DIR := A_ScriptDir
global PYTHON_PATH := FileExist(AGENT_DIR "\\python-portable\\python.exe")
    ? AGENT_DIR "\\python-portable\\python.exe"
    : "python"
global JarvisGui := 0

CreateGui(selectedText := "") {
    global JarvisGui
    if IsObject(JarvisGui)
        JarvisGui.Destroy()

    gui := Gui("+AlwaysOnTop -Caption", "Jampandu")
    gui.BackColor := "101010"
    gui.SetFont("s10", "Segoe UI")
    gui.AddText("x10 y10 w480 h20", "Jampandu - Suggestion (Esc closes)")
    original := gui.AddEdit("vOriginalText x10 y40 w480 r7")
    original.Value := selectedText
    checkButton := gui.AddButton("x10 y170 w90 h30", "Check")
    copyButton := gui.AddButton("x105 y170 w110 h30", "Copy Suggestion")
    pasteButton := gui.AddButton("x220 y170 w90 h30", "Paste Suggestion")
    cleanButton := gui.AddButton("x320 y170 w150 h30", "Stop && Clean Drive")
    gui.AddEdit("vSuggestion x10 y210 w480 r7 ReadOnly")

    checkButton.OnEvent("Click", RunQuery)
    copyButton.OnEvent("Click", CopySuggestion)
    pasteButton.OnEvent("Click", PasteSuggestion)
    cleanButton.OnEvent("Click", StopAndClean)
    gui.OnEvent("Escape", CloseGui)
    gui.OnEvent("Close", CloseGui)
    JarvisGui := gui
    gui.Show("w500 h340")
}

CloseGui(*) {
    global JarvisGui
    if IsObject(JarvisGui)
        JarvisGui.Destroy()
    JarvisGui := 0
}

RunQuery(*) {
    global AGENT_DIR, PYTHON_PATH, JarvisGui
    originalText := JarvisGui["OriginalText"].Value
    if !Trim(originalText) {
        MsgBox("No text selected or provided.", "Jampandu", 48)
        return
    }

    tempId := A_TickCount
    tempIn := AGENT_DIR "\\data\\tmp\\popup_in_" tempId ".txt"
    tempOut := AGENT_DIR "\\data\\tmp\\popup_out_" tempId ".txt"
    try {
        FileDelete(tempIn)
        FileDelete(tempOut)
        FileAppend(originalText, tempIn, "UTF-8")
        command := '"' PYTHON_PATH '" "' AGENT_DIR '\\single_query.py" --input-file "' tempIn '" --use_rag > "' tempOut '"'
        RunWait(A_ComSpec ' /d /c "' command '"', AGENT_DIR, "Hide")
        output := FileExist(tempOut) ? FileRead(tempOut, "UTF-8") : "(failed to read assistant output)"
        JarvisGui["Suggestion"].Value := output
    } catch as err {
        JarvisGui["Suggestion"].Value := "(query failed: " err.Message ")"
    } finally {
        try FileDelete(tempIn)
        try FileDelete(tempOut)
        A_Clipboard := ""
    }
}

CopySuggestion(*) {
    global JarvisGui
    suggestion := JarvisGui["Suggestion"].Value
    if !Trim(suggestion) {
        MsgBox("Nothing to copy.", "Jampandu", 48)
        return
    }
    A_Clipboard := suggestion
    MsgBox("Suggestion copied to clipboard.", "Jampandu", 64)
}

PasteSuggestion(*) {
    global JarvisGui
    suggestion := JarvisGui["Suggestion"].Value
    if !Trim(suggestion) {
        MsgBox("Nothing to paste.", "Jampandu", 48)
        return
    }
    A_Clipboard := suggestion
    Send("^v")
    SetTimer((*) => A_Clipboard := "", -250)
}

StopAndClean(*) {
    global AGENT_DIR
    try {
        RunWait(A_ComSpec ' /d /c ""' AGENT_DIR '\\stop_and_clean.bat""', AGENT_DIR, "Hide")
        MsgBox("Temporary files were removed.", "Jampandu", 64)
    } catch as err {
        MsgBox("Cleanup failed: " err.Message, "Jampandu", 16)
    }
}

^+j:: {
    savedClipboard := ClipboardAll()
    A_Clipboard := ""
    Send("^c")
    selectedText := ClipWait(0.5) ? A_Clipboard : ""
    A_Clipboard := savedClipboard
    CreateGui(selectedText)
}
