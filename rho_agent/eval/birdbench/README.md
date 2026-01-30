# BIRD-Bench Evaluation

Text-to-SQL evaluation on real multi-table SQLite databases using the [BIRD-Bench](https://bird-bench.github.io/) benchmark (NeurIPS 2023).

## Quick Start

```bash
uv run rho-eval bird \
~/proj/bird-bench-mini-dev/mini_dev_data/mini_dev_sqlite.json \
~/proj/bird-bench-mini-dev/mini_dev_data/dev_databases/ \
--model gpt-5-mini --service-tier flex --difficulty simple
```

## Data Setup

The data lives at `~/proj/bird-bench-mini-dev/`, a fork of `bird-bench/mini_dev`.

### 1. Clone the repo

```bash
cd ~/proj
git clone git@github.com:smith-nathanh/mini_dev.git bird-bench-mini-dev
```

### 2. Download the databases

The SQLite databases (~1.4 GB) are not in git. Download the zip from either link in `llm/mini_dev_data/README.md`:

- [Google Drive](https://drive.google.com/file/d/1UJyA6I6pTmmhYpwdn8iT9QKrcJqSQAcX/view?usp=sharing)
- [Alibaba Cloud](https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip)

Extract into the repo:

```bash
cd ~/proj/bird-bench-mini-dev
unzip minidev_0703.zip
mv minidev/MINIDEV mini_dev_data
rm -rf minidev __MACOSX
```

### Expected directory structure

```
~/proj/bird-bench-mini-dev/
  mini_dev_data/
    mini_dev_sqlite.json          # 500 tasks
    mini_dev_sqlite_gold.sql      # gold SQL (not used by our loader)
    dev_databases/                # 11 SQLite databases (~1.4 GB)
      california_schools/
        california_schools.sqlite
        database_description/*.csv
      card_games/
      codebase_community/
      debit_card_specializing/
      european_football_2/
      financial/
      formula_1/
      student_club/
      superhero/
      thrombosis_prediction/
      toxicology/
```

## CLI Reference

```
rho-eval bird <data_file> <db_dir> [OPTIONS]
```

| Argument | Description |
|----------|-------------|
| `data_file` | Path to task JSON (e.g., `mini_dev_sqlite.json`) |
| `db_dir` | Path to `dev_databases/` directory |

| Option | Default | Description |
|--------|---------|-------------|
| `--model, -m` | `gpt-5-mini` | Model name |
| `--base-url` | env `OPENAI_BASE_URL` | API endpoint |
| `--max-turns` | `20` | Max agent turns per task |
| `--parallel, -p` | `1` | Concurrent tasks |
| `--output, -o` | `results/<model>-bird/` | Output directory |
| `--resume, -r` | | Resume from a previous run directory |
| `--limit, -n` | | Run only N tasks |
| `--offset` | `0` | Skip first N tasks |
| `--difficulty` | | Filter: `simple`, `moderate`, or `challenging` |
| `--no-evidence` | | Withhold evidence hints (harder) |
| `--service-tier` | | OpenAI service tier (`flex`, `auto`) |
| `--verbose, -v` | | Verbose output |

### Examples

```bash
# Full mini-dev (500 tasks)
rho-eval bird mini_dev_sqlite.json dev_databases/ -m gpt-5-mini

# Only challenging tasks
rho-eval bird mini_dev_sqlite.json dev_databases/ --difficulty challenging

# Hard mode: no evidence hints
rho-eval bird mini_dev_sqlite.json dev_databases/ --no-evidence

# Parallel with flex tier
rho-eval bird mini_dev_sqlite.json dev_databases/ -p 4 --service-tier flex

# First 10 tasks only
rho-eval bird mini_dev_sqlite.json dev_databases/ -n 10

# Resume an interrupted run
rho-eval bird mini_dev_sqlite.json dev_databases/ --resume results/gpt-5-mini-bird/run-20260126-140000
```

## How It Works

### Task format

Each entry in `mini_dev_sqlite.json`:

```json
{
  "question_id": 0,
  "db_id": "california_schools",
  "question": "What is the highest eligible free rate for K-12 students in Alameda County?",
  "evidence": "Eligible free rate for K-12 = `Free Meal Count (K-12)` / `Enrollment (K-12)`",
  "SQL": "SELECT ... ORDER BY ... DESC LIMIT 1",
  "difficulty": "simple"
}
```

The agent sees the `question` and `evidence` (unless `--no-evidence`). It never sees the gold `SQL`.

### Agent tools

| Tool | Description |
|------|-------------|
| `execute_sql` | Run any SQL against the task's SQLite database. Used to explore schema, sample data, and test queries. |
| `submit_sql` | Submit the final SQL query for evaluation. |

The agent typically:
1. Discovers tables via `SELECT name FROM sqlite_master WHERE type='table'`
2. Inspects schemas via `PRAGMA table_info(table_name)`
3. Samples data to understand column values
4. Builds and tests the answer query
5. Submits via `submit_sql`

### Evaluation metric

**Execution Accuracy (EX)**: both the predicted and gold SQL are executed against the database; the task is correct if the result sets match (order-insensitive, NULLs equal).

```
EX = (# tasks where results match) / total_tasks
```

### Safety

The agent **cannot corrupt your databases**:

1. Each task copies the `.sqlite` file to a temp file. The agent works on the copy.
2. The evaluator opens the original database read-only (`file:...?mode=ro` URI).
3. Temp copies are deleted after each task.

## Output

Results are saved to `results/<model>-bird/run-<timestamp>/`:

| File | Contents |
|------|----------|
| `runs.jsonl` | One JSON object per task (history, result, status) |
| `overall.json` | Aggregate metrics with difficulty and database breakdowns |
| `summary.txt` | Human-readable summary |
| `config.json` | Run configuration for reproducibility |

### Sample summary output

```
=======================================================
BIRD-Bench Evaluation Results
=======================================================
Total tasks:     500
Passed (EX):     312
Failed:          188
Accuracy:        62.4%

By Difficulty:
  simple           120/148  (81.1%)
  moderate          170/250 (68.0%)
  challenging        22/102 (21.6%)

By Database:
  california_schools              25/30  (83.3%)
  card_games                      30/52  (57.7%)
  ...
=======================================================
```

## Module Structure

```
rho_agent/eval/birdbench/
  README.md          # this file
  design.md          # architecture rationale and open questions
  __init__.py
  task.py            # BirdTask dataclass + load_bird_tasks()
  tools.py           # BirdSqliteHandler (execute_sql) + SubmitSqlHandler (submit_sql)
  evaluator.py       # BirdEvaluator — runs both SQL, compares result sets
  runner.py          # BirdRunner — orchestrates task execution
  config.py          # EvalConfig, TaskResult, BirdMetrics
  output.py          # Result persistence and formatting
  cli.py             # CLI command definition
```

System prompt: `rho_agent/prompts/eval_bird.md`

## Dataset Stats

Mini-dev: **500 tasks** across **11 databases**.

### By difficulty

| Difficulty | Count | Share |
|------------|-------|-------|
| simple | 148 | 29.6% |
| moderate | 250 | 50.0% |
| challenging | 102 | 20.4% |

### By database x difficulty

| Database | Simple | Moderate | Challenging | Total |
|----------|--------|----------|-------------|-------|
| california_schools | 8 | 17 | 5 | 30 |
| card_games | 13 | 33 | 6 | 52 |
| codebase_community | 21 | 23 | 5 | 49 |
| debit_card_specializing | 14 | 12 | 4 | 30 |
| european_football_2 | 14 | 25 | 12 | 51 |
| financial | 3 | 22 | 7 | 32 |
| formula_1 | 28 | 26 | 12 | 66 |
| student_club | 21 | 22 | 5 | 48 |
| superhero | 14 | 26 | 12 | 52 |
| thrombosis_prediction | 7 | 27 | 16 | 50 |
| toxicology | 5 | 17 | 18 | 40 |

Hardest-skewing: **toxicology** (45% challenging), **thrombosis_prediction** (32% challenging).
Easiest-skewing: **formula_1** (42% simple), **codebase_community** (43% simple).
