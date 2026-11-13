#!/usr/bin/env bash
# Re-record the README demos against a real model.
#
#   docs/demo/record.sh cli       interactive session (docs/demo/cli.gif)
#   docs/demo/record.sh monitor   control plane (docs/demo/monitor.gif)
#
# Needs vhs (https://github.com/charmbracelet/vhs), ffmpeg, a project virtualenv
# (uv sync --group dev) and OPENAI_API_KEY in the environment or in .env.
# Everything runs in a throwaway home directory (/tmp/rho-agent-demo unless
# DEMO_HOME is set), so your own sessions and config never appear in a recording.
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$repo"
if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
: "${OPENAI_API_KEY:?set OPENAI_API_KEY or add it to .env}"

# The agents work on copies of the sample projects, never in this repository.
# Remember its state so a stray write is caught when the recording ends.
repo_state() { git -C "$repo" status --porcelain -- . ':!docs/demo/*.gif' ':!docs/demo/*.mp4'; }
repo_before="$(repo_state)"
check_repo() {
  if [[ "$(repo_state)" != "$repo_before" ]]; then
    echo "WARNING: files in the repository changed during the recording:" >&2
    diff <(echo "$repo_before") <(repo_state) >&2 || true
    exit 1
  fi
}

# A short path, because the agent's tool calls show absolute paths on screen.
demo_home="${DEMO_HOME:-/tmp/rho-agent-demo}"
if [[ -e "$demo_home" ]]; then
  # Only ever remove a directory this script created.
  [[ -f "$demo_home/.rho-agent-demo" ]] || { echo "$demo_home exists and is not a demo home" >&2; exit 1; }
  rm -rf "$demo_home"
fi
mkdir -p "$demo_home/proj"
touch "$demo_home/.rho-agent-demo"
demo_home="$(cd "$demo_home" && pwd -P)"  # the real path, so projects show as ~/proj/...
cp -R docs/demo/projects/. "$demo_home/proj/"
cp docs/demo/careful.yaml "$demo_home/careful.yaml"

# The tapes switch HOME to this directory inside the recorded shell.
export DEMO_HOME="$demo_home"
export PATH="$repo/.venv/bin:$PATH"
export BASH_SILENCE_DEPRECATION_WARNING=1

# to_gif <name> [trim]: turn docs/demo/<name>.mp4 into docs/demo/<name>.gif.
# With "trim", cut a few seconds after the screen last changed, for a tape
# that ends with a long idle wait.
to_gif() {
  local mp4="docs/demo/$1.mp4" gif="docs/demo/$1.gif" idle_from length
  idle_from="$(ffmpeg -hide_banner -i "$mp4" -vf "freezedetect=n=0.001:d=5" -map 0:v -f null - 2>&1 |
    grep -o 'freeze_start: [0-9.]*' | tail -1 | cut -d' ' -f2 || true)"
  length="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$mp4")"
  if [[ "${2:-}" == trim && -n "$idle_from" ]]; then
    length="$(echo "$idle_from + 6" | bc)"
  fi
  ffmpeg -hide_banner -loglevel error -y -t "$length" -i "$mp4" \
    -vf "fps=10,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=none" "$gif"
  rm "$mp4"
  echo "wrote $gif ($(du -h "$gif" | cut -f1), ${length}s)"
}

case "${1:-}" in
  cli)
    vhs docs/demo/cli.tape
    to_gif cli trim
    check_repo
    ;;
  monitor)
    # Start two unattended agents in the demo home. With no terminal attached
    # they queue their approval requests for the monitor.
    agent() {  # agent <project> <prompt>
      (cd "$demo_home/proj/$1" && HOME="$demo_home" nohup rho-agent main \
        --profile "$demo_home/careful.yaml" "$2" >"$demo_home/$1.log" 2>&1 </dev/null &)
    }
    agent billing "Customers report that split payments do not add up to the invoice \
total. Reproduce it with the tests, fix invoice.py, and run the tests again."
    agent api-server "Review this service before release. Use the delegate tool to run two \
reviews in parallel: one of auth.py for security problems, and one of whether the tests \
cover every route in routes.py (that reviewer should run the test suite). Then summarise."

    sessions="$demo_home/.config/rho-agent/sessions"
    session_for() {  # session_for <project> -> session ID
      local meta
      meta="$(grep -l "\"cwd\": \".*/proj/$1\"" "$sessions"/*/meta.json 2>/dev/null | head -1 || true)"
      [[ -n "$meta" ]] && basename "$(dirname "$meta")" || true
    }
    echo "waiting for both agents to ask for approval..."
    for _ in $(seq 1 120); do
      billing="$(session_for billing)"; api="$(session_for api-server)"
      if [[ -n "$billing" && -n "$api" ]] &&
        ls "$sessions/$billing"/approvals/*.json >/dev/null 2>&1 &&
        ls "$sessions/$api"/approvals/*.json >/dev/null 2>&1; then
        break
      fi
      sleep 2
    done
    : "${billing:?billing agent did not start}" "${api:?api-server agent did not start}"

    tape="$(mktemp -t monitor).tape"
    sed -e "s/BILLING/${billing:0:4}/g" -e "s/API/${api:0:4}/g" docs/demo/monitor.tape >"$tape"
    vhs "$tape"
    to_gif monitor
    HOME="$demo_home" rho-agent cancel --all >/dev/null || true
    check_repo
    ;;
  *)
    echo "usage: $0 cli|monitor" >&2
    exit 2
    ;;
esac
