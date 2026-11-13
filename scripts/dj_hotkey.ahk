#Requires AutoHotkey v2.0
#SingleInstance Force

RControl::F13

DJ_PATH := A_ScriptDir "\..\data\dj_input.txt"

SendDJ(value)
{
    Run('cmd /c echo ' value ' > "' DJ_PATH '"', , "Hide")
}

; Standard-Mix; Musikwünsche direkt über Dashboard oder F13 + V.
F13 & 1::SendDJ(5)
F13 & 2::SendDJ("skip")
F13 & 3::SendDJ("ban-artist")
F13 & 4::SendDJ("ban")
F13 & 5::SendDJ("quit")
