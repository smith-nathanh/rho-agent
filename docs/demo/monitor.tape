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
Set Theme { "name": "Carbon", "black": "#121212", "red": "#F87171", "green": "#6EE7B7", "yellow": "#E5B869", "blue": "#60A5FA", "magenta": "#A3A3A3", "cyan": "#60A5FA", "white": "#EAEAEA", "brightBlack": "#555555", "brightRed": "#F87171", "brightGreen": "#6EE7B7", "brightYellow": "#E5B869", "brightBlue": "#60A5FA", "brightMagenta": "#D4D4D4", "brightCyan": "#60A5FA", "brightWhite": "#FAFAFA", "background": "#121212", "foreground": "#EAEAEA", "selection": "#242424", "cursor": "#60A5FA" }
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
