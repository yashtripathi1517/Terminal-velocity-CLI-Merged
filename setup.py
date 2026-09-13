"""
repopilot setup — scan the current repo, detect dependency files,
generate the right install commands, and optionally run them.

Design:
  1. Rule-based lookup table is the PRIMARY path.
  2. Local LLM (Ollama) is an OPTIONAL fallback for ambiguous files.
  3. Works fully offline — LLM is never required.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
OLLAMA_MODEL = "llama3"
OLLAMA_TIMEOUT_S = 5  # seconds

# Rule-based lookup: filename → (project_type, install_command)
# Order matters — earlier entries are executed first when multiple match.
DEPENDENCY_MAP = {
    "requirements.txt": ("Python (pip)",       "pip install -r requirements.txt"),
    "pyproject.toml":   ("Python (pyproject)", "pip install -e ."),
    "setup.py":         ("Python (setup.py)",  "pip install -e ."),
    "package.json":     ("Node.js",            "npm install"),
    "go.mod":           ("Go",                 "go mod tidy"),
    "pom.xml":          ("Java (Maven)",       "mvn install"),
    "build.gradle":     ("Java (Gradle)",      "gradle build"),
    "Gemfile":          ("Ruby",               "bundle install"),
    "Cargo.toml":       ("Rust",               "cargo build"),
    "composer.json":    ("PHP (Composer)",      "composer install"),
    "Dockerfile":       ("Docker",             None),  # needs dir name
    "Makefile":         ("Make",               "make"),
    "CMakeLists.txt":   ("CMake",              "cmake -B build && cmake --build build"),
    "build.bat":        ("Batch Script",       "cmd /c build.bat"),
    "build.sh":         ("Shell Script",       "bash build.sh"),
    "setup.sh":         ("Shell Script",       "bash setup.sh"),
    "install.sh":       ("Shell Script",       "bash install.sh"),
    "install.bat":      ("Batch Script",       "cmd /c install.bat"),
}

# Files to ignore when searching for ambiguous build scripts to send to the LLM
IGNORE_EXTENSIONS = {
    ".py", ".js", ".ts", ".html", ".css", ".csv", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".lock", ".log",
    ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".pdf", ".zip", ".tar", ".gz", ".mp3", ".mp4", ".wav", ".sql"
}


# ---------------------------------------------------------------------
# register() — called from main.py, DO NOT CHANGE signature
# ---------------------------------------------------------------------

def register(subparsers):
    """Register the 'setup' subcommand with its arguments."""
    parser = subparsers.add_parser(
        "setup",
        help="Scan the repo, detect dependencies, and run install commands.",
        description=(
            "Scan the current (or specified) directory for dependency files "
            "(requirements.txt, package.json, go.mod, etc.), show the "
            "install commands that would be run, and optionally execute them."
        ),
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt and run commands immediately.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print detected commands without executing them.",
    )
    parser.add_argument(
        "--dir", "-d",
        default=".",
        metavar="PATH",
        help="Target directory to scan (defaults to current directory).",
    )
    parser.set_defaults(handler=handle)


# ---------------------------------------------------------------------
# handle() — the core logic
# ---------------------------------------------------------------------

def handle(args):
    """
    Phase 1: Scan → Phase 2: Resolve → Phase 3: Confirm → Phase 4: Execute
    """
    target_dir = os.path.abspath(args.dir)

    # Validate directory
    if not os.path.isdir(target_dir):
        _error(f"directory '{args.dir}' does not exist or is not a directory")
        sys.exit(1)

    # ── Phase 1: Scan ──────────────────────────────────────────────
    detected = _scan(target_dir)

    if not detected:
        _error("no recognizable dependency file found in the current directory")
        sys.exit(1)

    # ── Phase 2: Resolve ───────────────────────────────────────────
    plan = _resolve(detected, target_dir)

    if not plan:
        _error("detected files could not be resolved to install commands")
        sys.exit(1)

    # ── Phase 3: Confirm ───────────────────────────────────────────
    _print_plan(plan)

    if args.dry_run:
        sys.exit(0)

    if not args.yes:
        if not _confirm():
            print("Aborted.")
            sys.exit(0)

    # ── Phase 4: Execute ───────────────────────────────────────────
    has_failure = _execute(plan, target_dir)
    sys.exit(1 if has_failure else 0)


# ---------------------------------------------------------------------
# Phase 1 — Scan
# ---------------------------------------------------------------------

def _scan(target_dir):
    """
    List files in target_dir and classify each as:
      - 'rule'  → present in DEPENDENCY_MAP
      - 'llm'   → present in LLM_CANDIDATE_FILES (needs fallback)

    Returns a list of (filename, category) tuples.
    """
    try:
        entries = os.listdir(target_dir)
    except PermissionError:
        _error(f"permission denied reading '{target_dir}'")
        sys.exit(1)

    detected = []

    # Check rule-based files first (preserves DEPENDENCY_MAP order)
    for filename in DEPENDENCY_MAP:
        if filename in entries:
            detected.append((filename, "rule"))

    # Check for generic script extensions as rule fallbacks or LLM candidates
    for filename in sorted(entries):
        if filename in [d[0] for d in detected]:
            continue
            
        filepath = os.path.join(target_dir, filename)
        if not os.path.isfile(filepath) or filename.startswith("."):
            continue
            
        ext = os.path.splitext(filename)[1].lower()
        
        # Rule fallback for common script extensions
        if ext in (".bat", ".cmd"):
            detected.append((filename, "rule_script_bat"))
        elif ext in (".sh", ".bash"):
            detected.append((filename, "rule_script_sh"))
        elif ext in (".ps1",):
            detected.append((filename, "rule_script_ps1"))
        elif ext not in IGNORE_EXTENSIONS:
            detected.append((filename, "llm"))

    return detected


# ---------------------------------------------------------------------
# Phase 2 — Resolve
# ---------------------------------------------------------------------

def _resolve(detected, target_dir):
    """
    Convert detected files into a concrete plan:
      [(project_type, command, filename), ...]

    Rule-based files use DEPENDENCY_MAP.
    LLM-candidate files attempt the Ollama fallback.
    """
    plan = []
    seen_commands = set()  # de-duplicate identical commands

    for filename, category in detected:
        if category == "rule":
            project_type, cmd_template = DEPENDENCY_MAP[filename]

            # Special case: Dockerfile needs the directory name for the tag
            if cmd_template is None and filename == "Dockerfile":
                dir_name = os.path.basename(target_dir) or "app"
                # Sanitise the tag: lowercase, alphanumeric + hyphens only
                tag = re.sub(r"[^a-z0-9\-]", "", dir_name.lower()) or "app"
                cmd = f"docker build -t {tag} ."
            else:
                cmd = cmd_template

            if cmd in seen_commands:
                continue
            seen_commands.add(cmd)
            plan.append((project_type, cmd, filename))

        elif category.startswith("rule_script_"):
            if category == "rule_script_bat":
                cmd = f"cmd /c {filename}"
                ptype = "Batch Script"
            elif category == "rule_script_sh":
                cmd = f"bash {filename}"
                ptype = "Shell Script"
            elif category == "rule_script_ps1":
                cmd = f"powershell .\\{filename}"
                ptype = "PowerShell Script"
            
            if cmd not in seen_commands:
                seen_commands.add(cmd)
                plan.append((ptype, cmd, filename))

        elif category == "llm":
            result = _llm_resolve(filename, target_dir)
            if result:
                project_type, cmd = result
                if cmd not in seen_commands:
                    seen_commands.add(cmd)
                    plan.append((project_type, cmd, filename))

    return plan


def _llm_resolve(filename, target_dir):
    """
    Attempt to resolve an ambiguous file via local Ollama LLM.
    Returns (project_type, command) or None on any failure.
    """
    filepath = os.path.join(target_dir, filename)
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            # Read first 200 lines to keep the prompt small
            content_lines = []
            for i, line in enumerate(f):
                if i >= 200:
                    break
                content_lines.append(line)
            content = "".join(content_lines)
    except (OSError, IOError):
        return None

    import platform
    os_name = platform.system()
    
    system_prompt = (
        "You are a build-system expert. The user will show you a file from a "
        "software project. Respond with ONLY the single shell command needed to "
        "install dependencies or build the project. No explanation, no markdown "
        "fences, no commentary — just the raw command. "
        f"The user's operating system is {os_name}. Provide a command that works on {os_name}. "
        "If you cannot determine the command, respond with exactly: UNKNOWN"
    )

    user_prompt = (
        f"File: {filename}\n"
        f"Contents (first 200 lines):\n"
        f"```\n{content}\n```\n"
        f"What single shell command should I run?"
    )

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 100,
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        raw = body["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, urllib.error.HTTPError,
            OSError, KeyError, IndexError, json.JSONDecodeError,
            TimeoutError) as exc:
        print(
            f"  ⚠  LLM fallback unavailable for {filename} ({type(exc).__name__}), skipping.",
            file=sys.stderr,
        )
        return None

    # Sanitise: strip markdown code fences if the model wrapped them
    cmd = _sanitise_llm_response(raw)
    if cmd is None:
        return None

    project_type = f"{filename} (LLM-resolved)"
    return (project_type, cmd)


def _sanitise_llm_response(raw):
    """
    Clean up an LLM response to extract a single shell command.
    Returns the command string, or None if the response is unusable.
    """
    if not raw or raw.upper() == "UNKNOWN":
        return None

    # Strip markdown code fences: ```bash ... ``` or ``` ... ```
    cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
    cleaned = re.sub(r"\n?```$", "", cleaned)
    cleaned = cleaned.strip()

    # Reject multi-line responses (likely an explanation, not a command)
    lines = [l for l in cleaned.splitlines() if l.strip()]
    if len(lines) != 1:
        return None

    cmd = lines[0].strip()

    # Basic safety: reject empty or suspiciously dangerous commands
    if not cmd:
        return None
    dangerous_patterns = [
        r"\brm\s+(-rf?|--recursive)", r"\bsudo\b", r"\bdd\b",
        r"\bmkfs\b", r">\s*/dev/", r"\bformat\b",
    ]
    for pat in dangerous_patterns:
        if re.search(pat, cmd, re.IGNORECASE):
            print(
                f"  [!] LLM suggested a potentially dangerous command, skipping: {cmd}",
                file=sys.stderr,
            )
            return None

    return cmd


# ---------------------------------------------------------------------
# Phase 3 — Confirm
# ---------------------------------------------------------------------

def _print_plan(plan):
    """Pretty-print the detected projects and their commands."""
    print()
    print("  RepoPilot Setup -- Detected Projects")
    print("  " + "-" * 40)
    for i, (project_type, cmd, filename) in enumerate(plan, 1):
        print(f"  {i}. [{project_type}]  {filename}")
        print(f"     -> {cmd}")
    print("  " + "-" * 40)
    print()


def _confirm():
    """Prompt the user for y/n confirmation. Returns True on 'y'."""
    try:
        answer = input("  Proceed with installation? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


# ---------------------------------------------------------------------
# Phase 4 — Execute
# ---------------------------------------------------------------------

def _execute(plan, target_dir):
    """
    Run each command in sequence. Returns True if any command failed.
    """
    has_failure = False

    for project_type, cmd, filename in plan:
        print(f"  > Running: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=target_dir,
            )
            if result.returncode == 0:
                print(f"  [OK] {project_type} setup complete")
            else:
                print(f"  [FAIL] {project_type} setup failed (exit {result.returncode})")
                _error(f"command '{cmd}' exited with code {result.returncode}")
                has_failure = True
        except OSError as exc:
            print(f"  [FAIL] {project_type} setup failed ({exc})")
            _error(f"failed to run '{cmd}': {exc}")
            has_failure = True

    print()
    if has_failure:
        print("  Some steps failed. Review the output above.")
    else:
        print("  All steps completed successfully.")

    return has_failure


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _error(msg):
    """Print a formatted error to stderr per the shared convention."""
    print(f"Error: {msg}. Run 'repopilot setup --help' for usage.", file=sys.stderr)
