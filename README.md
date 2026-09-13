# RepoPilot

RepoPilot is a unified CLI tool for developer workflows, built during the "Can You Hack It?" hackathon (Terminal Velocity track). 
It integrates multiple environment, setup, log analysis, and branch cleaning utilities into a single cohesive interface.

## Installation

You can install the package directly from the source code:

```bash
pip install git+https://github.com/Vikhyaat-Srivastava/Terminal-velocity-CLI-Merged.git
```

## Usage

RepoPilot provides four primary subcommands: `setup`, `env`, `clean`, and `logs`.

```bash
repopilot --help
```

### 1. Setup (`repopilot setup`)

Scans the repository for dependency files (`package.json`, `requirements.txt`, `go.mod`, etc.) and automatically generates the appropriate install commands. Includes a local LLM fallback for ambiguous files.

```bash
# Scan current directory and prompt for confirmation before running
repopilot setup

# Run silently without prompt
repopilot setup --yes

# Only print what would be run
repopilot setup --dry-run
```

### 2. Environment Management (`repopilot env`)

Manage and switch environment variables safely. `env switch` outputs shell commands that are evaluated by the parent shell to actually alter your current terminal environment.

```bash
# List available environments (looks for .env.* files)
repopilot env list

# Switch to a specific environment
# Evaluated via eval:
eval $(repopilot env switch staging)

# See the currently active environment
repopilot env current
```
*Note: Because a child process cannot modify the parent shell's environment, `repopilot env switch` prints shell commands to STDOUT. You must use `eval` (or equivalent) in your shell.*

### 3. Branch Cleanup (`repopilot clean`)

Finds and deletes local git branches that have already been merged. Prevents deleting protected branches (`main`, `master`, `dev`) and your current working branch.

```bash
# Preview what would be deleted
repopilot clean --dry-run

# Run and confirm before deleting
repopilot clean

# Run without confirmation
repopilot clean --force

# Specify custom protected branches
repopilot clean --protect main develop stable
```

### 4. Log Analysis (`repopilot logs`)

Filter and optionally summarize logs from a file, a Docker container, or `journalctl`.

```bash
# Tail logs from a file and filter by level
repopilot logs --file app.log --level error

# Tail logs from a docker container since 1 hour ago
repopilot logs --docker my-container --since 1h

# Trawl journalctl for a specific pattern
repopilot logs --journalctl --grep "OOM"

# Use local LLM (Ollama) to summarize the filtered output
repopilot logs --docker my-container --level error --summarize
```

## Exit Codes

All subcommands adhere to the following exit codes:
* `0`: Success
* `1`: User error (invalid arguments, missing files, user aborted)
* `2`: Internal error (unexpected exceptions, tool failures)

## Hackathon Assets

Reference implementations, examples, and agent logs used during the hackathon are preserved in `hackathon-tooling/` and `examples/`.
