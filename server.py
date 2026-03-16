"""
Local learning server — serves the web app and provides API for state files.
Usage: python3 server.py [port] [--claude-path /path/to/claude]
Default port: 3000
"""

import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).parent

# Parse CLI args: python3 server.py [port] [--claude-path /path/to/claude]
PORT = 3000
CLAUDE_PATH_OVERRIDE = None
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == "--claude-path" and i + 1 < len(args):
        CLAUDE_PATH_OVERRIDE = args[i + 1]
        i += 2
    elif args[i].isdigit():
        PORT = int(args[i])
        i += 1
    else:
        i += 1

# Track running generation threads and subprocesses
_generation_lock = threading.Lock()
_generating = {"calibration": False, "curriculum": False}
_active_processes = []  # list of Popen objects
_cancel_generation = threading.Event()


def _resolve_claude_cmd():
    """Determine the claude CLI command to use.
    Priority: --claude-path flag > claude-code (shell fn) > claude binary.
    """
    if CLAUDE_PATH_OVERRIDE:
        return CLAUDE_PATH_OVERRIDE

    # Check if claude-code shell function exists (defined in ~/.zshrc)
    check = subprocess.run(
        'source ~/.zshrc 2>/dev/null; type claude-code &>/dev/null && echo found',
        shell=True, capture_output=True, text=True,
        executable=shutil.which("zsh") or "/bin/zsh",
    )
    if "found" in check.stdout:
        return "claude-code"

    # Fall back to claude binary
    if shutil.which("claude"):
        return "claude"

    print("\n  ERROR: Neither 'claude-code' nor 'claude' CLI found.")
    print("  Please run with: python3 server.py --claude-path /path/to/claude\n")
    sys.exit(1)


CLAUDE_CMD = _resolve_claude_cmd()
print(f"  Using CLI: {CLAUDE_CMD}")


def _update_generation_progress(steps, detail=None):
    """Write generation progress for the UI progress bar.
    Frontend expects: {label: str, done: bool} for each step.
    Optional detail: a real-time status message shown under the active step.
    """
    ui_steps = [{"label": s["label"], "done": s["done"]} for s in steps]
    data = {"started": True, "steps": ui_steps}
    if detail:
        data["detail"] = detail
    (BASE / "GENERATION_PROGRESS.json").write_text(
        json.dumps(data, indent=2)
    )


def _kill_active_processes():
    """Kill all active claude subprocesses."""
    for proc in _active_processes[:]:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
    _active_processes.clear()


def _run_claude(prompt, allowed_tools="Read,Write,Edit,Bash"):
    """Run claude CLI in print mode and return stdout. Returns None if cancelled."""
    if _cancel_generation.is_set():
        return None

    tools_flag = f'--allowedTools {allowed_tools} ' if allowed_tools else ''
    cmd = (
        f'source ~/.zshrc 2>/dev/null; '
        f'{shlex.quote(CLAUDE_CMD)} -p '
        f'{tools_flag}'
        f'--permission-mode bypassPermissions '
        f'--no-session-persistence '
        f'{shlex.quote(prompt)}'
    )
    print(f"  [{CLAUDE_CMD}] Running: {prompt[:80]}...")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(BASE), shell=True,
        executable=shutil.which("zsh") or "/bin/zsh",
    )
    _active_processes.append(proc)
    try:
        stdout, stderr = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    finally:
        if proc in _active_processes:
            _active_processes.remove(proc)

    if _cancel_generation.is_set():
        return None

    if proc.returncode != 0:
        print(f"  [{CLAUDE_CMD}] Error: {stderr[:500]}")
    else:
        print(f"  [{CLAUDE_CMD}] Done ({len(stdout)} chars)")

    # Return a result-like object
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    return result


def _generate_calibration(topic):
    """Background: ask Claude to generate calibration questions."""
    with _generation_lock:
        if _generating["calibration"]:
            return
        _generating["calibration"] = True
    _cancel_generation.clear()

    try:
        if _cancel_generation.is_set():
            return
        prompt = f"""The user wants to learn: {topic}

Generate 2-3 calibration questions to gauge their starting level. These must be SPECIFIC to {topic} — they should test whether the user actually knows foundational concepts, not ask how familiar they are.

Write the result to CALIBRATION.json with this exact format:
{{
  "ready": true,
  "intro": "A few questions to figure out where you are with {topic}.",
  "questions": [
    {{
      "question": "A specific knowledge-testing question",
      "hint": "A helpful placeholder hint",
      "type": "short"
    }}
  ]
}}

Write ONLY the CALIBRATION.json file. Do not create any other files."""

        _run_claude(prompt, allowed_tools="Write")
    finally:
        with _generation_lock:
            _generating["calibration"] = False


def _parse_module_nums_from_curriculum():
    """Parse module numbers from CURRICULUM.md module map table."""
    cur_file = BASE / "CURRICULUM.md"
    if not cur_file.exists():
        return []
    text = cur_file.read_text()
    nums = []
    for m in re.finditer(
        r"\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*\d+\s*\|", text
    ):
        nums.append((int(m.group(1)), m.group(2).strip()))
    return nums


def _generate_curriculum(topic):
    """Background: generate curriculum in phases with granular progress."""
    with _generation_lock:
        if _generating["curriculum"]:
            return
        _generating["curriculum"] = True
    _cancel_generation.clear()

    try:
        if _cancel_generation.is_set():
            return
        answers_file = BASE / "CALIBRATION_ANSWERS.json"
        answers_text = answers_file.read_text() if answers_file.exists() else "{}"

        # --- Phase 1a: Generate CURRICULUM.md ---
        _update_generation_progress([
            {"label": "Analyzing calibration answers", "done": True},
            {"label": "Designing module structure & cards", "done": False},
            {"label": "Setting up progress tracker", "done": False},
            {"label": "Preparing lessons", "done": False},
        ], detail="Reading project instructions and planning curriculum...")

        prompt_curriculum = f"""The user wants to learn: {topic}

Their calibration answers:
{answers_text}

You MUST now generate the curriculum. Follow the CLAUDE.md instructions precisely for format.

Do the following steps:
1. Read CLAUDE.md to understand the exact formats required.
2. Create the lessons/ directory if it doesn't exist (use Bash: mkdir -p lessons).
3. Write CURRICULUM.md with 10-12 modules organized into tiers (Foundations, Core, Fluency, Mastery). Each module needs 6-8 cards across Concept, Compute, Visual, and Coding types (Coding cards for technical topics only). Include the Module Map Table at the top. For every Coding card, include a Solution: field with a complete reference solution.

Use the calibration answers to determine starting level.

IMPORTANT: Only generate CURRICULUM.md in this step. Do NOT generate PROGRESS.md or lesson files yet."""

        # Monitor file activity while Claude generates the curriculum
        _curriculum_monitor_stop = threading.Event()

        def _monitor_curriculum_progress():
            """Update detail messages based on file activity during curriculum generation."""
            import time
            base_steps = [
                {"label": "Analyzing calibration answers", "done": True},
                {"label": "Designing module structure & cards", "done": False},
                {"label": "Setting up progress tracker", "done": False},
                {"label": "Preparing lessons", "done": False},
            ]
            messages = [
                (5, "Reading project instructions..."),
                (15, "Analyzing topic structure and prerequisites..."),
                (30, "Organizing modules into learning tiers..."),
                (50, "Writing card definitions and assessment probes..."),
                (80, "Detailing module content..."),
                (120, "Still working — large curriculum takes a moment..."),
                (180, "Almost there — finalizing module details..."),
            ]
            start = time.time()
            while not _curriculum_monitor_stop.is_set():
                elapsed = time.time() - start
                # Pick the latest matching message
                detail = "Generating curriculum structure..."
                for threshold, msg in messages:
                    if elapsed >= threshold:
                        detail = msg
                # Check if CURRICULUM.md has started appearing
                cur_file = BASE / "CURRICULUM.md"
                if cur_file.exists():
                    size_kb = cur_file.stat().st_size / 1024
                    if size_kb > 1:
                        detail = f"Writing curriculum ({size_kb:.0f} KB so far)..."
                _update_generation_progress(base_steps, detail=detail)
                _curriculum_monitor_stop.wait(3)

        monitor = threading.Thread(target=_monitor_curriculum_progress, daemon=True)
        monitor.start()

        _run_claude(prompt_curriculum, allowed_tools="Read,Write,Edit,Bash")
        _curriculum_monitor_stop.set()

        if _cancel_generation.is_set():
            return

        _update_generation_progress([
            {"label": "Analyzing calibration answers", "done": True},
            {"label": "Designing module structure & cards", "done": True},
            {"label": "Setting up progress tracker", "done": False},
            {"label": "Preparing lessons", "done": False},
        ])

        # --- Phase 1b: Generate PROGRESS.md ---
        _update_generation_progress([
            {"label": "Analyzing calibration answers", "done": True},
            {"label": "Designing module structure & cards", "done": True},
            {"label": "Setting up progress tracker", "done": False},
            {"label": "Preparing lessons", "done": False},
        ], detail="Building SRS schedule and module status table...")

        prompt_progress = f"""The user wants to learn: {topic}

Read the CURRICULUM.md file that was just generated.

Write PROGRESS.md with the progress tracker table matching every module from the curriculum. Follow the exact format from CLAUDE.md.

Use these calibration answers to determine which Tier 1 modules to mark as "assessed — skip" vs "active":
{answers_text}

IMPORTANT: Only generate PROGRESS.md. Do not modify CURRICULUM.md or create lesson files."""

        _run_claude(prompt_progress, allowed_tools="Read,Write")
        if _cancel_generation.is_set():
            return

        _update_generation_progress([
            {"label": "Analyzing calibration answers", "done": True},
            {"label": "Designing module structure & cards", "done": True},
            {"label": "Setting up progress tracker", "done": True},
            {"label": "Preparing lessons", "done": False},
        ])

        # --- Phase 2: Parse modules and generate lessons one-by-one ---
        modules = _parse_module_nums_from_curriculum()
        if not modules:
            print("  [generation] Warning: no modules found in CURRICULUM.md")
            modules = [(i, f"Module {i}") for i in range(1, 11)]

        # Build full progress steps list
        steps = [
            {"label": "Analyzing calibration answers", "done": True},
            {"label": "Designing module structure & cards", "done": True},
            {"label": "Setting up progress tracker", "done": True},
        ]
        for num, name in modules:
            steps.append({"label": f"Writing lesson {num}: {name}", "done": False})
        steps.append({"label": "Finalizing", "done": False})
        _update_generation_progress(steps)

        # Read curriculum for context in lesson prompts
        curriculum_text = (BASE / "CURRICULUM.md").read_text() if (BASE / "CURRICULUM.md").exists() else ""

        for i, (num, name) in enumerate(modules):
            if _cancel_generation.is_set():
                print("  [generation] Cancelled by user")
                return
            lesson_file = BASE / f"lessons/module_{num}.md"
            if lesson_file.exists():
                print(f"  [generation] Lesson {num} already exists, skipping")
            else:
                prompt_lesson = f"""You are generating lesson content for a learning curriculum on: {topic}

Here is the full curriculum for context (module structure, cards, prereqs):
---
{curriculum_text}
---

Generate the lesson file for Module {num}: {name}.
Write it to lessons/module_{num}.md.

The lesson should be readable in 5-10 minutes and include:
1. Why this matters — motivate the topic, connect to the bigger picture
2. Concept explanations — clear prose with examples, not just definitions
3. Worked examples — step-by-step walkthroughs
4. Common pitfalls — what beginners get wrong and why
5. Key takeaways — concise summary

Write as a tutor, not a textbook. Use concrete examples before abstract rules.
End with: "Head to SRS Review or Teach Back to solidify this."

Write ONLY the file lessons/module_{num}.md. Do not modify any other files."""

                _run_claude(prompt_lesson, allowed_tools="Write")

            # Mark this lesson done
            steps[3 + i]["done"] = True
            _update_generation_progress(steps)

        # --- Phase 3: Finalize ---
        steps[-1]["done"] = True
        _update_generation_progress(steps)

        # Clear request files
        for f in ["TOPIC_REQUEST.json", "CALIBRATION.json", "CALIBRATION_ANSWERS.json"]:
            p = BASE / f
            if p.exists():
                p.unlink()

        print(f"  [claude-cli] Curriculum generation complete for '{topic}'")

    except Exception as e:
        print(f"  [claude-cli] Curriculum generation failed: {e}")
        _update_generation_progress([
            {"label": f"Error: {e}", "done": False},
        ])
    finally:
        with _generation_lock:
            _generating["curriculum"] = False


def read_file(name):
    p = BASE / name
    return p.read_text() if p.exists() else None


def write_file(name, content):
    p = BASE / name
    p.write_text(content)


def parse_progress(text):
    if not text:
        return {"modules": [], "total_cards": 0, "mastered": 0, "session_count": 0, "history": []}
    info = {"modules": [], "total_cards": 0, "mastered": 0, "session_count": 0, "history": []}

    for m in re.finditer(
        r"\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*([\w —]+?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*(\d+)\s*\|",
        text,
    ):
        info["modules"].append({
            "num": int(m.group(1)),
            "name": m.group(2).strip(),
            "cards": int(m.group(3)),
            "status": m.group(4).strip(),
            "last_review": m.group(5).strip(),
            "next_review": m.group(6).strip(),
            "streak": int(m.group(7)),
        })

    tc = re.search(r"Total cards:\s*(\d+)", text)
    if tc:
        info["total_cards"] = int(tc.group(1))
    mc = re.search(r"Mastered:\s*(\d+)/(\d+)", text)
    if mc:
        info["mastered"] = int(mc.group(1))
    sc = re.search(r"Current session:\s*(\d+)", text)
    if sc:
        info["session_count"] = int(sc.group(1))

    history_section = text.split("## Card History")[-1] if "## Card History" in text else ""
    for line in history_section.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("Format:") or line.startswith("-"):
            continue
        m = re.match(r"\[?([\d-]+)\]?\s+([\d.]+\w*)\s*\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(\d+)", line)
        if m:
            info["history"].append({
                "date": m.group(1),
                "card": m.group(2),
                "type": m.group(3),
                "result": m.group(4),
                "streak": int(m.group(5)),
            })

    return info


def parse_curriculum(text):
    if not text:
        return {"modules": [], "raw": ""}
    info = {"modules": [], "raw": text}

    # Parse module map table
    for m in re.finditer(
        r"\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|",
        text,
    ):
        num = int(m.group(1))
        info["modules"].append({
            "num": num,
            "name": m.group(2).strip(),
            "cards": int(m.group(3)),
            "prereqs": m.group(4).strip(),
            "focus": m.group(5).strip(),
        })

    # Parse detailed module sections with cards
    module_sections = re.split(r"(?=## Module \d+)", text)
    for section in module_sections:
        header = re.match(r"## Module (\d+):\s*(.+)", section)
        if not header:
            continue
        mod_num = int(header.group(1))

        # Find the module in our list
        mod = next((m for m in info["modules"] if m["num"] == mod_num), None)
        if not mod:
            continue

        # Parse status and prereqs from the section
        status_m = re.search(r"Status:\s*(.+?)(?:\s*\||\n)", section)
        if status_m:
            mod["detail_status"] = status_m.group(1).strip()

        # Parse cards
        mod["card_details"] = []
        for card_m in re.finditer(
            r"\*\*(\d+\.\d+)\s*\[(\w+)\]\*\*\s*(.+?)(?:\n|$)(.*?)(?=\*\*\d+\.\d+|\Z)",
            section,
            re.DOTALL,
        ):
            card = {
                "id": card_m.group(1),
                "type": card_m.group(2),
                "title": card_m.group(3).strip(),
                "body": card_m.group(4).strip(),
            }
            q_match = re.search(r"Q:\s*(.+?)(?:\n|$)", card["body"])
            if q_match:
                card["question"] = q_match.group(1).strip()
            v_match = re.search(r"Validation:\s*(.+?)(?:\n|$)", card["body"])
            if v_match:
                card["validation"] = v_match.group(1).strip()
            # Parse coding card fields
            sol_match = re.search(r"Solution:\s*(.+?)(?:\n- |\Z)", card["body"], re.DOTALL)
            if sol_match:
                card["solution"] = sol_match.group(1).strip()
            lang_match = re.search(r"Language:\s*(.+?)(?:\n|$)", card["body"])
            if lang_match:
                card["language"] = lang_match.group(1).strip()
            con_match = re.search(r"Constraints:\s*(.+?)(?:\n|$)", card["body"])
            if con_match:
                card["constraints"] = con_match.group(1).strip()
            mod["card_details"].append(card)

        # Parse assessment probes
        mod["probes"] = []
        for probe_m in re.finditer(r"\*\*\d+\.P(\d+)\*\*\s*(.+?)(?:\n|$)", section):
            mod["probes"].append({
                "num": int(probe_m.group(1)),
                "question": probe_m.group(2).strip(),
            })

    return info


def parse_profile(text):
    if not text:
        return None
    info = {"errors": [], "strengths": [], "preferences": [], "notes": []}

    for m in re.finditer(
        r"\|\s*(\S+.*?)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|",
        text,
    ):
        name = m.group(1).strip()
        if name.lower() in ("pattern", "---", "-"):
            continue
        info["errors"].append({
            "pattern": name,
            "count": int(m.group(2)),
            "first": m.group(3).strip(),
            "last": m.group(4).strip(),
            "example": m.group(5).strip(),
        })

    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if "## Strengths" in line:
            section = "strengths"
        elif "## Verified Preferences" in line:
            section = "preferences"
        elif "## Session Notes" in line:
            section = "notes"
        elif stripped.startswith("##"):
            section = None
        elif stripped.startswith("- ") and section in info:
            info[section].append(stripped[2:])

    return info


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/state":
            self._json_response(self._get_all_state())
        elif path == "/api/curriculum":
            text = read_file("CURRICULUM.md")
            self._json_response(parse_curriculum(text))
        elif path == "/api/progress":
            text = read_file("PROGRESS.md")
            self._json_response(parse_progress(text))
        elif path == "/api/profile":
            text = read_file("LEARNER_PROFILE.md")
            self._json_response(parse_profile(text))
        elif path == "/api/curriculum/raw":
            text = read_file("CURRICULUM.md")
            self._text_response(text or "")
        elif path == "/api/progress/raw":
            text = read_file("PROGRESS.md")
            self._text_response(text or "")
        elif path == "/api/profile/raw":
            text = read_file("LEARNER_PROFILE.md")
            self._text_response(text or "")
        elif path.startswith("/api/lesson/"):
            # /api/lesson/1 → lessons/module_1.md
            mod_num = path.split("/")[-1]
            lesson_file = f"lessons/module_{mod_num}.md"
            text = read_file(lesson_file)
            if text:
                self._json_response({"module": int(mod_num), "content": text, "exists": True})
            else:
                self._json_response({"module": int(mod_num), "content": None, "exists": False})
        elif path == "/api/lessons":
            # List all available lessons
            lessons_dir = BASE / "lessons"
            available = []
            if lessons_dir.exists():
                for f in lessons_dir.glob("module_*.md"):
                    num = int(f.stem.replace("module_", ""))
                    available.append(num)
            available.sort()
            self._json_response({"available": available})
        elif path == "/api/topic":
            topic_file = BASE / "TOPIC_REQUEST.json"
            if topic_file.exists():
                self._json_response(json.loads(topic_file.read_text()))
            else:
                self._json_response({"pending": False})
        elif path == "/api/calibration":
            cal_file = BASE / "CALIBRATION.json"
            if cal_file.exists():
                self._json_response(json.loads(cal_file.read_text()))
            else:
                self._json_response({"ready": False})
        elif path == "/api/generation-progress":
            gp_file = BASE / "GENERATION_PROGRESS.json"
            if gp_file.exists():
                self._json_response(json.loads(gp_file.read_text()))
            else:
                self._json_response({"steps": [], "started": False})
        elif path == "/" or path == "/index.html":
            self._serve_file("index.html", "text/html")
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""

        if path == "/api/progress/raw":
            write_file("PROGRESS.md", body)
            self._json_response({"ok": True})
        elif path == "/api/profile/raw":
            write_file("LEARNER_PROFILE.md", body)
            self._json_response({"ok": True})
        elif path == "/api/progress/append-history":
            data = json.loads(body)
            self._append_history(data)
            self._json_response({"ok": True})
        elif path == "/api/progress/update-module":
            data = json.loads(body)
            self._update_module(data)
            self._json_response({"ok": True})
        elif path == "/api/topic":
            data = json.loads(body)
            data["pending"] = True
            data["timestamp"] = datetime.now().isoformat()
            (BASE / "TOPIC_REQUEST.json").write_text(json.dumps(data, indent=2))
            self._json_response({"ok": True})
            # Trigger calibration generation in background
            if data.get("stage") == "needs_calibration":
                topic = data.get("topic", "")
                threading.Thread(
                    target=_generate_calibration, args=(topic,), daemon=True
                ).start()
        elif path == "/api/calibration":
            data = json.loads(body)
            (BASE / "CALIBRATION_ANSWERS.json").write_text(json.dumps(data, indent=2))
            # Update topic request stage
            topic_file = BASE / "TOPIC_REQUEST.json"
            topic = ""
            if topic_file.exists():
                tr = json.loads(topic_file.read_text())
                tr["stage"] = "ready_for_generation"
                topic = tr.get("topic", "")
                topic_file.write_text(json.dumps(tr, indent=2))
            self._json_response({"ok": True})
            # Trigger curriculum generation in background
            if topic:
                threading.Thread(
                    target=_generate_curriculum, args=(topic,), daemon=True
                ).start()
        elif path == "/api/topic/clear":
            # Clean up all request files
            for f in ["TOPIC_REQUEST.json", "CALIBRATION.json", "CALIBRATION_ANSWERS.json", "GENERATION_PROGRESS.json"]:
                p = BASE / f
                if p.exists():
                    p.unlink()
            self._json_response({"ok": True})
        elif path == "/api/coding/feedback":
            data = json.loads(body)
            user_code = data.get("code", "")
            question = data.get("question", "")
            validation = data.get("validation", "")
            solution = data.get("solution", "")
            language = data.get("language", "")
            constraints = data.get("constraints", "")

            if user_code == "__GENERATE_SOLUTION__":
                prompt = f"""You are a coding tutor. Write a clean, correct reference solution for this problem.

- Question: {question}
- Language: {language}
- Constraints: {constraints}
- Validation criteria: {validation}

Provide ONLY the code solution with brief inline comments explaining key decisions. Use markdown code blocks. After the code, add 2-3 sentences explaining the approach and its time/space complexity."""
            else:
                prompt = f"""You are a strict but constructive code reviewer for a learning system.

The student was given this coding problem:
- Question: {question}
- Language: {language}
- Constraints: {constraints}
- Validation criteria: {validation}
- Reference solution: {solution}

The student wrote:
```
{user_code}
```

Give focused feedback in this exact format (use markdown):
1. **Correctness** — Does it produce the right output? If not, give a specific failing input.
2. **Edge cases** — What inputs would break it? List any unhandled cases.
3. **Complexity** — Does it meet the constraints? If not, what's the actual complexity and how to fix it?
4. **Style** — Any clarity issues? Keep this brief — only flag genuine obscurity.
5. **Verdict** — One line: Correct / Partially correct / Incorrect, with the key reason.

If the code is correct and clean, say so briefly — don't invent issues. Be specific, not generic. Do NOT rewrite their code or provide the full solution — give hints that help them fix it themselves."""

            # Run claude in a thread to not block
            def get_feedback():
                result = _run_claude(prompt, allowed_tools="")
                if result and hasattr(result, 'stdout'):
                    return result.stdout.strip()
                return None

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(get_feedback)
                try:
                    result = future.result(timeout=120)
                    self._json_response({"ok": True, "feedback": result or "No feedback generated."})
                except concurrent.futures.TimeoutError:
                    self._json_response({"ok": False, "feedback": "Feedback timed out."})
        elif path == "/api/reset":
            # Cancel any ongoing generation and kill subprocesses
            _cancel_generation.set()
            _kill_active_processes()
            with _generation_lock:
                _generating["calibration"] = False
                _generating["curriculum"] = False
            # Delete all generated state to start fresh
            for f in [
                "CURRICULUM.md", "PROGRESS.md", "LEARNER_PROFILE.md",
                "TOPIC_REQUEST.json", "CALIBRATION.json",
                "CALIBRATION_ANSWERS.json", "GENERATION_PROGRESS.json",
            ]:
                p = BASE / f
                if p.exists():
                    p.unlink()
            # Remove all lesson files
            lessons_dir = BASE / "lessons"
            if lessons_dir.exists():
                for f in lessons_dir.glob("module_*.md"):
                    f.unlink()
            # Remove all visuals
            visuals_dir = BASE / "visuals"
            if visuals_dir.exists():
                for f in visuals_dir.iterdir():
                    if f.is_file():
                        f.unlink()
            print("  [reset] All state cleared, generation cancelled")
            self._json_response({"ok": True})
        else:
            self.send_error(404)

    def _get_all_state(self):
        return {
            "curriculum": parse_curriculum(read_file("CURRICULUM.md")),
            "progress": parse_progress(read_file("PROGRESS.md")),
            "profile": parse_profile(read_file("LEARNER_PROFILE.md")),
            "has_curriculum": (BASE / "CURRICULUM.md").exists(),
            "has_progress": (BASE / "PROGRESS.md").exists(),
            "has_profile": (BASE / "LEARNER_PROFILE.md").exists(),
            "today": datetime.now().strftime("%Y-%m-%d"),
        }

    def _append_history(self, data):
        """Append a line to the Card History section of PROGRESS.md."""
        text = read_file("PROGRESS.md")
        if not text:
            return
        line = data.get("line", "")
        if "## Card History" in text:
            text = text.rstrip() + "\n" + line + "\n"
        else:
            text = text.rstrip() + "\n\n## Card History\n\n" + line + "\n"
        write_file("PROGRESS.md", text)

    def _update_module(self, data):
        """Update a module row in PROGRESS.md."""
        text = read_file("PROGRESS.md")
        if not text:
            return
        mod_num = data.get("num")
        updates = data.get("updates", {})

        lines = text.splitlines()
        new_lines = []
        for line in lines:
            m = re.match(r"\|\s*(\d+)\s*\|", line)
            if m and int(m.group(1)) == mod_num:
                # Parse the existing row
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 7:
                    if "status" in updates:
                        parts[3] = updates["status"]
                    if "last_review" in updates:
                        parts[4] = updates["last_review"]
                    if "next_review" in updates:
                        parts[5] = updates["next_review"]
                    if "streak" in updates:
                        parts[6] = str(updates["streak"])
                    line = "| " + " | ".join(parts) + " |"
            new_lines.append(line)

        # Update mastered count
        new_text = "\n".join(new_lines)
        mastered = sum(1 for mod_line in new_lines
                       if re.search(r"\|\s*mastered\s*\|", mod_line))
        new_text = re.sub(r"Mastered:\s*\d+/", f"Mastered: {mastered}/", new_text)

        # Update session count
        if "increment_session" in updates:
            new_text = re.sub(
                r"Current session:\s*(\d+)",
                lambda m: f"Current session: {int(m.group(1)) + 1}",
                new_text,
            )

        write_file("PROGRESS.md", new_text)

    def _json_response(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _text_response(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename, content_type):
        p = BASE / filename
        if not p.exists():
            self.send_error(404)
            return
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Quieter logging
        if "/api/" in str(args[0]):
            return
        super().log_message(format, *args)


def _find_available_port(start=3000, max_tries=100):
    """Find the next available port starting from `start`."""
    import socket
    for port in range(start, start + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No available port found in range {start}-{start + max_tries}")


if __name__ == "__main__":
    port = _find_available_port(PORT)
    if port != PORT:
        print(f"  Port {PORT} in use, using {port} instead")
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"\n  SRS Learning Server running at http://localhost:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
