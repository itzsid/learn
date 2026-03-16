"""CLI entry points for learn-tutor."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
LEARN_HOME = Path.home() / "learn"

DATA_FILES = ["server.py", "index.html", "CLAUDE.md", "LEARNING_THEORY.md"]


def _find_open_port(start=3000, attempts=100):
    """Find an available port starting from `start`."""
    import socket

    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def _start_server(topic_dir):
    """Start the server in topic_dir and open the browser."""
    port = _find_open_port()
    proc = subprocess.Popen(
        [sys.executable, "server.py", str(port)],
        cwd=str(topic_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    time.sleep(1.5)

    if proc.poll() is not None:
        out = proc.stdout.read().decode() if proc.stdout else ""
        print(f"Server failed to start:\n{out}", file=sys.stderr)
        sys.exit(1)

    url = f"http://localhost:{port}"
    print(f"  Topic:  {topic_dir.name}")
    print(f"  URL:    {url}")
    print(f"  Folder: {topic_dir}")
    print()

    # Open browser
    if sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
    elif sys.platform == "linux":
        subprocess.run(["xdg-open", url], check=False)

    print("Press Ctrl+C to stop the server.")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait(timeout=5)
        print("\nServer stopped.")


def learn():
    """Create a new topic folder and start learning."""
    if len(sys.argv) < 2:
        print("Usage: learn <topic>")
        print()
        print("Examples:")
        print("  learn rust")
        print("  learn music theory")
        print("  learn linear algebra")
        print()
        print(f"Topics are stored in {LEARN_HOME}/")

        # List existing topics
        if LEARN_HOME.exists():
            topics = sorted(
                d.name
                for d in LEARN_HOME.iterdir()
                if d.is_dir() and (d / "server.py").exists()
            )
            if topics:
                print()
                print("Existing topics:")
                for t in topics:
                    print(f"  {t}")
        sys.exit(1)

    topic = "-".join(sys.argv[1:]).lower()
    topic_dir = LEARN_HOME / topic

    # Create topic folder and copy data files
    topic_dir.mkdir(parents=True, exist_ok=True)
    for f in DATA_FILES:
        src = DATA_DIR / f
        dst = topic_dir / f
        if src.exists():
            shutil.copy2(src, dst)

    print(f"Starting learn session...\n")
    _start_server(topic_dir)


def start():
    """Resume a learning session in the current directory."""
    cwd = Path.cwd()

    if not (cwd / "server.py").exists():
        print("Not a learn folder.", file=sys.stderr)
        print()
        print("Either:")
        print("  1. cd into a topic folder and run: start")
        print("  2. Create a new topic with: learn <topic>")
        sys.exit(1)

    print(f"Resuming learn session...\n")
    _start_server(cwd)
