# Interactive session: ask the agent to find the cause of two failing tests.
# Record with docs/demo/record.sh cli (it prepares the sample project).

Output docs/demo/cli.mp4

Set Shell bash
Set FontSize 15
Set Width 1100
Set Height 720
Set Padding 20
Set TypingSpeed 40ms
Set CursorBlink false
# The default frame rate cannot keep up and makes the video play fast.
Set Framerate 20

Hide
Type "export HOME=$DEMO_HOME RHO_AGENT_PREVIEW_LINES=3 && cd ~/proj/shop && export PS1='\[\e[2m\]shop\[\e[0m\] $ ' && clear"
Enter
Show

Type "pytest -q --tb=no"
Enter
Sleep 3s

Type "rho-agent --profile developer"
Enter
Sleep 4s

Type "two checkout tests are failing. find the root cause, fix it, and run the tests"
Sleep 500ms
Enter
# Longer than the agent needs. record.sh cuts the recording a few seconds
# after the screen stops changing.
Sleep 75s
