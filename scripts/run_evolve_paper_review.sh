#!/usr/bin/env bash
# Evolve loop: paper review domain (HyperAgents benchmark)
#
# Evolves a task-agent that predicts accept/reject for ML conference papers.
# The meta-agent (gpt-5.4) iteratively improves the task-agent's prompt and
# tools. The task-agent (gpt-5.4-mini) is evaluated on each generation.
#
# Runs inside a Daytona sandbox so the meta-agent cannot read the dataset
# or ground truth directly.

set -euo pipefail

uv run rho-agent evolve \
  rho_agent.evolve.harnesses.paper_review.PaperReviewHarness \
  --run-dir ./evolve-runs/paper-review-5.4 \
  --model gpt-5.4 \
  --task-model gpt-5.4-mini \
  --daytona \
  \
  --max-generations 20 \
  \
  --harness-arg train_n=100 \
  \
  --harness-arg val_n=100 \
  \
  --staged-sample 10 \
  \
  --parent-strategy score_child_prop \
  \
  # train_n=100        100 papers used for full evaluation each generation.
  #                    Score is accuracy on this set.
  #
  # val_n=100          100 separate papers (disjoint from train) used for
  #                    staged filtering. Not used for the final score.
  #
  # staged-sample=10   Before running the full 100-paper eval, test on 10
  #                    papers from the val set. If the score is <50% of the
  #                    parent's score, skip the full eval (saves cost).
  #
  # parent-strategy    score_child_prop: select parents proportional to
  #                    their score, inversely weighted by how many children
  #                    they already have. Balances exploitation (pick high
  #                    scorers) with exploration (try under-explored nodes).
  #
  # max-generations    20 iterations of: select parent -> meta-agent mutates
  #                    workspace -> staged eval -> full eval -> archive.
