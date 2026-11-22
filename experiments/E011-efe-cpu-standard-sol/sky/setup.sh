#!/usr/bin/env bash
# VM setup for E011: setup.sh <harbor config>. Docker with Compose, uv,
# rho-agent at the pinned commit (Harbor's host side), and the Embodied First
# Exam tasks at the leaderboard's commit, stamped. Ends with a Harbor dry run of
# the config, so a bad config fails the launch before any model call.
# Adapted from E005's sky/setup.sh. Safe to rerun.
set -euo pipefail
config=$1

RHO_AGENT_COMMIT=f9354038686fea71ebca0a95aec26af2d8ea130b   # main after smith-nathanh/rho-agent#38
EFE_COMMIT=ebd10b62b0f4d5c299c3249555d2746e9eb51880         # the leaderboard snapshot's task set
# The suites the config runs, from its dataset paths (../efe/tasks/<suite>).
EFE_SUITES=$(grep -o '\.\./efe/tasks/[A-Za-z0-9_-]*' "$config" | sed 's|^\.\./efe/||' | sort -u | tr '\n' ' ')
[ -n "$EFE_SUITES" ] || { echo "no ../efe/tasks/<suite> paths in $config" >&2; exit 1; }
echo "EFE suites: $EFE_SUITES"
# Harbor's allowlist check runs this image with a 30 s limit on pull and run;
# a slower first pull reads as "allowlist unsupported" for the whole job.
PROBE_IMAGE=alpine:3.23.4@sha256:5b10f432ef3da1b8d4c7eb6c487f2f5a8f096bc91145e68878dd4a5019afde11

if ! docker compose version >/dev/null 2>&1; then
  sudo apt-get update -q
  sudo apt-get install -y -q docker.io docker-compose-v2
fi
sudo systemctl enable --now docker
# A single-user VM that is deleted after the run: let the SkyPilot user reach
# Docker without a new login session for the docker group.
sudo chmod 666 /var/run/docker.sock
docker pull -q "$PROBE_IMAGE"

command -v uv >/dev/null 2>&1 || [ -x "$HOME/.local/bin/uv" ] \
  || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

[ -d ~/rho-agent ] || git clone -q https://github.com/smith-nathanh/rho-agent ~/rho-agent
cd ~/rho-agent
git fetch -q origin
git checkout -q "$RHO_AGENT_COMMIT"
uv sync --frozen --extra harbor
uv run harbor --version

# EFE is 760 MB in full: fetch only the commit's tree for the suites run here
# (blobs on demand), plus scripts/ for stamping.
if [ ! -d ~/efe/.git ]; then
  git init -q ~/efe
  git -C ~/efe remote add origin https://github.com/mll-lab-nu/embodied-first-exam.git
fi
cd ~/efe
# Add to what earlier jobs on this VM checked out (another segment's suites),
# so a later resume of that segment still finds its tasks.
if git sparse-checkout list >/dev/null 2>&1 && [ -n "$(git sparse-checkout list 2>/dev/null)" ]; then
  git sparse-checkout add $EFE_SUITES scripts
else
  git sparse-checkout set --cone $EFE_SUITES scripts
fi
git fetch -q --depth 1 --filter=blob:none origin "$EFE_COMMIT"
git checkout -q --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "$EFE_COMMIT"
# EFE requires content-derived mtimes before building task images: BuildKit can
# otherwise reuse another task's equal-size file (EFE issue #258).
# (rho-agent's venv Python, 3.12+: the VM image's python3 may be older.)
~/rho-agent/.venv/bin/python -I scripts/stamp_mtimes.py tasks
~/rho-agent/.venv/bin/python -I scripts/stamp_mtimes.py tasks --check

cd ~/rho-agent
uv run harbor run --config "$config" --dry-run
