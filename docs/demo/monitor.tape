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
Set Theme { "name": "Stone", "black": "#141312", "red": "#E07A7A", "green": "#6FC297", "yellow": "#E5B472", "blue": "#A8A29E", "magenta": "#D97757", "cyan": "#A8A29E", "white": "#E6E2DC", "brightBlack": "#6B6560", "brightRed": "#E07A7A", "brightGreen": "#6FC297", "brightYellow": "#E5B472", "brightBlue": "#D6D1CA", "brightMagenta": "#D97757", "brightCyan": "#D6D1CA", "brightWhite": "#F5F2ED", "background": "#141312", "foreground": "#E6E2DC", "selection": "#282522", "cursor": "#D97757" }
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
