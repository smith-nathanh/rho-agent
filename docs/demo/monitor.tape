# Control plane: two agents working unattended, supervised from one terminal.
# Record with docs/demo/record.sh monitor. It starts the agents, waits until
# they are blocked on approvals, and fills in BILLING and API (session IDs).

Output docs/demo/monitor.mp4

Set Shell bash
Set FontSize 15
Set Width 1200
Set Height 820
Set Padding 20
Set TypingSpeed 40ms
Set CursorBlink false
# The default frame rate cannot keep up and makes the video play fast.
Set Framerate 20

Hide
Type "export HOME=$DEMO_HOME && cd ~ && export PS1='$ ' && clear"
Enter
Show

Type "rho-agent monitor"
Enter
Sleep 5s

Type "approvals"
Enter
Sleep 4s

Type "approve all"
Enter
Sleep 2.5s

Type "watch BILLING"
Enter
Sleep 14s
Ctrl+C
Sleep 1s

Type "directive API also list any route that skips the auth check"
Enter
Sleep 3s

Type "ps"
Enter
Sleep 6s

Type "quit"
Enter
Sleep 1s
