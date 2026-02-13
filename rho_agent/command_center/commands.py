from __future__ import annotations

from dataclasses import dataclass

from rho_agent.command_center.services.control_plane import ControlPlane


@dataclass(slots=True)
class ParsedCommand:
    name: str
    target_prefix: str | None = None
    text: str = ""


def _split_words(text: str) -> list[str]:
    return [w for w in text.strip().split() if w]


def parse_palette_command(raw: str, *, control_plane: ControlPlane) -> ParsedCommand:
    """Parse palette input.

    Supports commands:
      pause [prefix]
      resume [prefix]
      kill [prefix]
      directive <prefix> <text...>
      launch
      refresh

    Also supports prefix resolution for the command name itself.
    """

    s = raw.strip()
    if s.startswith("/"):
        s = s[1:]
    parts = _split_words(s)
    if not parts:
        raise ValueError("No command")

    cmd_token = parts[0].lower()
    rest = parts[1:]

    commands = ["pause", "resume", "kill", "directive", "launch", "refresh"]
    matches = [c for c in commands if c.startswith(cmd_token)]
    if not matches:
        raise ValueError(f"Unknown command '{cmd_token}'")
    if len(matches) > 1:
        # If token exactly matches one, prefer it.
        if cmd_token in matches:
            cmd = cmd_token
        else:
            raise ValueError(
                f"Ambiguous command '{cmd_token}'; could be: {', '.join(sorted(matches))}"
            )
    else:
        cmd = matches[0]

    if cmd in {"pause", "resume", "kill"}:
        prefix = rest[0] if rest else "all"
        # Reuse ControlPlane resolution patterns for error messages.
        resolved = control_plane.resolve_running_prefix(prefix)
        if not resolved:
            raise ValueError(f"No running agents matching '{prefix}'")
        return ParsedCommand(name=cmd, target_prefix=prefix)

    if cmd == "directive":
        if len(rest) < 2:
            raise ValueError("Usage: directive <session-prefix> <text>")
        prefix = rest[0]
        text = " ".join(rest[1:]).strip()
        session_id, err = control_plane.resolve_single_running(prefix)
        if err:
            raise ValueError(err)
        if session_id is None:
            raise ValueError(f"No running agents matching '{prefix}'")
        return ParsedCommand(name=cmd, target_prefix=prefix, text=text)

    if cmd in {"launch", "refresh"}:
        if rest:
            raise ValueError(f"Usage: {cmd}")
        return ParsedCommand(name=cmd)

    raise ValueError(f"Unhandled command '{cmd}'")
