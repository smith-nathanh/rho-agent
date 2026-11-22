#!/usr/bin/env bash
# Runs one Harbor job on the VM: run.sh <job name> <config>. The job name is
# the last part of the config's jobs_dir. Same as E005's sky/run.sh.
#
# Results are copied to the GCS bucket mounted at /results every 5 minutes and
# when the job ends. If a Spot VM was preempted, relaunching the cluster copies
# the job back from the bucket and resumes it: Harbor reruns only the trials
# that had not finished.
set -euo pipefail
name=$1
config=$2
export PATH="$HOME/.local/bin:$PATH"
cd ~/rho-agent
mkdir -p jobs /results

if [ -d "/results/$name" ] && [ ! -d "jobs/$name" ]; then
  cp -r "/results/$name" "jobs/$name"
fi

save() { [ ! -d "jobs/$name" ] || rsync -rt --inplace "jobs/$name/" "/results/$name/" || true; }
( while sleep 300; do save; done ) &
saver=$!
# Stop the loop and its sleep, so the SkyPilot job ends when Harbor does.
trap 'pkill -P $saver 2>/dev/null; kill $saver 2>/dev/null; save' EXIT

# EFE tasks read environment variables (RCB_LIMITS_ENFORCE and others, in
# [verifier.env]), and Harbor asks before loading them. There is no terminal
# here, so the prompt aborts the job: run confirms with --yes; resume has no
# such flag, so it gets "y" on stdin.
previous=$(ls -d "jobs/$name"/*/ 2>/dev/null | sort | tail -1 || true)
if [ -n "$previous" ]; then
  echo "Resuming $previous"
  uv run harbor job resume -p "$previous" < <(yes)
else
  uv run harbor run --config "$config" --yes
fi
