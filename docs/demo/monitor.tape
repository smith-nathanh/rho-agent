# Control plane: two agents across two projects (with parallel subagents),
# supervised from the monitor shell (`ps` + `inspect`) and Live Mission Control (`live`).
# Record with docs/demo/record.sh monitor. It starts the agents, waits until
# they are blocked on approvals, and fills in BILLING and API (session IDs).

Output docs/demo/monitor.mp4

Set Shell bash
Set FontSize 15
Set Width 1240
Set Height 820
Set Padding 22
Set TypingSpeed 40ms
Set CursorBlink false
Set Theme { "name": "Glacier", "black": "#101822", "red": "#FF969D", "green": "#7AD8AE", "yellow": "#FFBF79", "blue": "#78C8FF", "magenta": "#B9B7F5", "cyan": "#9DB4C5", "white": "#E7F3FA", "brightBlack": "#8EA7BA", "brightRed": "#FF969D", "brightGreen": "#7AD8AE", "brightYellow": "#F1CB8F", "brightBlue": "#A0D9FF", "brightMagenta": "#CBC8FF", "brightCyan": "#C4D7E4", "brightWhite": "#F3FAFF", "background": "#101822", "foreground": "#E7F3FA", "selection": "#2A4763", "cursor": "#78C8FF" }
Set Framerate 20

Hide
Type "export HOME=$DEMO_HOME && cd ~ && export PS1='$ ' && alias rho-agent='exec rho-agent' && clear"
Enter
Show

Type "rho-agent monitor"
Enter
Sleep 4s

# Inspect the parent session's full history, subagent tree, and pending approval
Type "inspect API"
Enter
Sleep 5s

# Switch into Live Mission Control
Type "live"
Enter
Sleep 3s

# Navigate through the fleet & subagent tree while watching the right-hand Inspector
Down
Sleep 2s
Down
Sleep 2s
Down
Sleep 2.5s

# Approve waiting agents right inside Live Mission Control
Type "a"
Sleep 2s
Type "A"
Sleep 2.5s

# Return to the top live session and inject a mid-run directive
Up
Up
Up
Sleep 1s
Type "d"
Sleep 1s
Type "summarize any edge cases you noticed in two bullets"
Enter
Sleep 5s

Type "q"
Sleep 1s
