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
Set Theme { "name": "Stone", "black": "#141312", "red": "#E07A7A", "green": "#6FC297", "yellow": "#E5B472", "blue": "#A8A29E", "magenta": "#D97757", "cyan": "#A8A29E", "white": "#E6E2DC", "brightBlack": "#6B6560", "brightRed": "#E07A7A", "brightGreen": "#6FC297", "brightYellow": "#E5B472", "brightBlue": "#D6D1CA", "brightMagenta": "#D97757", "brightCyan": "#D6D1CA", "brightWhite": "#F5F2ED", "background": "#141312", "foreground": "#E6E2DC", "selection": "#282522", "cursor": "#D97757" }
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

Type "node-04 keeps resetting in alerts.log, yet watchdog.py reports nominal. Correlate alerts.log with telemetry.db and policy.toml, add the missing check to watchdog.py, and run it to verify."
Sleep 500ms
Enter
# Wait for the agent to finish its investigation, edit, and verification cleanly.
Sleep 20s

Type "/status"
Enter
Sleep 5s
