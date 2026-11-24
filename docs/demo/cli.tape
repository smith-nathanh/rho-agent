# Interactive session: investigate watchdog resets across logs + SQLite telemetry,
# patch watchdog.py against policy.toml, and verify against the live database.
# Record with docs/demo/record.sh cli (it prepares the sample project & telemetry.db).

Output docs/demo/cli.mp4

Set Shell bash
Set FontSize 15
Set Width 1120
Set Height 740
Set Padding 22
Set TypingSpeed 40ms
Set CursorBlink false
Set Theme { "name": "Glacier", "black": "#101822", "red": "#FF969D", "green": "#7AD8AE", "yellow": "#FFBF79", "blue": "#78C8FF", "magenta": "#B9B7F5", "cyan": "#9DB4C5", "white": "#E7F3FA", "brightBlack": "#8EA7BA", "brightRed": "#FF969D", "brightGreen": "#7AD8AE", "brightYellow": "#F1CB8F", "brightBlue": "#A0D9FF", "brightMagenta": "#CBC8FF", "brightCyan": "#C4D7E4", "brightWhite": "#F3FAFF", "background": "#101822", "foreground": "#E7F3FA", "selection": "#2A4763", "cursor": "#78C8FF" }
# The default frame rate cannot keep up and makes the video play fast.
Set Framerate 20

Hide
# exec: if the session exits early, the recording ends with it, and nothing
# typed later runs in a shell.
Type "export HOME=$DEMO_HOME RHO_AGENT_PREVIEW_LINES=5 && cd ~/proj/telemetry && export PS1='\[\e[2m\]~/telemetry\[\e[0m\] $ ' && alias rho-agent='exec rho-agent' && clear"
Enter
Show

Type "python3 watchdog.py"
Enter
Sleep 2s

Type "rho-agent"
Enter
Sleep 3.5s

Type "Why does watchdog.py miss the node-04 resets? Check alerts.log, policy.toml, and the node-04 readings in SQLite. Patch watchdog.py and run it to verify."
Sleep 500ms
Enter
# Wait for the agent to finish its investigation, edit, and verification cleanly.
Sleep 16s

Type "/status"
Enter
Sleep 5s
