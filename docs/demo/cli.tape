# Interactive session: ask the agent to find and fix two failing obstacle-stop tests.
# Record with docs/demo/record.sh cli (it prepares the sample project).

Output docs/demo/cli.mp4

Set Shell bash
Set FontSize 15
Set Width 1120
Set Height 740
Set Padding 22
Set TypingSpeed 40ms
Set CursorBlink false
Set Theme { "name": "Carbon", "black": "#121212", "red": "#F87171", "green": "#6EE7B7", "yellow": "#E5B869", "blue": "#60A5FA", "magenta": "#A3A3A3", "cyan": "#60A5FA", "white": "#EAEAEA", "brightBlack": "#555555", "brightRed": "#F87171", "brightGreen": "#6EE7B7", "brightYellow": "#E5B869", "brightBlue": "#60A5FA", "brightMagenta": "#D4D4D4", "brightCyan": "#60A5FA", "brightWhite": "#FAFAFA", "background": "#121212", "foreground": "#EAEAEA", "selection": "#242424", "cursor": "#60A5FA" }
# The default frame rate cannot keep up and makes the video play fast.
Set Framerate 20

Hide
# exec: if the session exits early, the recording ends with it, and nothing
# typed later runs in a shell.
Type "export HOME=$DEMO_HOME RHO_AGENT_PREVIEW_LINES=3 && cd ~/proj/rover && export PS1='\[\e[2m\]rover\[\e[0m\] $ ' && alias rho-agent='exec rho-agent' && clear"
Enter
Show

Type "pytest -q --tb=no"
Enter
Sleep 2.5s

Type "rho-agent"
Enter
Sleep 3.5s

Type "two obstacle-stop tests are failing. find the root cause, fix it, and run the tests"
Sleep 500ms
Enter
# Wait for the agent to finish its turn cleanly before checking /status.
Sleep 42s

Type "/status"
Enter
Sleep 4s
