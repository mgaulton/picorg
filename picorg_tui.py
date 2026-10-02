#!/usr/bin/env python3
"""Split-pane PicOrg terminal UI."""

from __future__ import annotations

import curses
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MENU = [
    ("1", "New intake + safe review", [["./run_picorg.sh"]], False),
    ("2", "Safe scan (no intake)", [["./run_picorg.sh", "--dry-run"]], False),
    ("3", "Review existing face groups", [["./run_picorg.sh", "--no-ingest", "--reuse-faces"]], False),
    ("4", "Apply high-confidence moves (benchmark-gated)", [["bash", "-lc", "set -Eeuo pipefail; BENCH=\"${FACE_BENCHMARK_REPORT:-.cache/picorg/image-face-calibration.json}\"; if [[ ! -s \"$BENCH\" ]]; then echo \"ERROR: benchmark report required before moves: $BENCH; run option b first\"; exit 2; fi; FACE_BENCHMARK_REPORT=\"$BENCH\" ./run_picorg.sh --apply-high-confidence"]], True),
    ("5", "Quarantine exact duplicates", [["./run_picorg.sh", "--no-ingest", "--dedupe-apply", "--reuse-faces"]], True),
    ("6", "Stop face rebuild", None, True),
    ("7", "Rebuild identity gallery (slow)", [["./rebuild_face_data.sh"]], True),
    ("8", "Existing DB -> gallery -> match -> clusters -> UI", [["./run_existing_face_db.sh"]], False),
    ("9", "Full accuracy rebuild + review", [["./rebuild_face_data.sh"], ["bash", "-lc", "FORCE_FACE_REBUILD=1 USE_EXISTING_FACE_AUDIT=0 ./runweb.sh"]], True),
    ("g", "Build canonical face baseline (read-only sources)", [["./build_canonical_face_baseline.sh"]], False),
    ("f", "Full pipeline: ingest -> names -> face DB -> match -> UI", [["./run_picorg.sh", "--apply-high-confidence"]], True),
    ("b", "Benchmark confirmed faces (no moves)", [["bash", "./run_accuracy_benchmark.sh"]], False),
    ("c", "Cluster references by identity (review-only)", [["bash", "-lc", "set -Eeuo pipefail; CACHE=\"${INSIGHTFACE_REFERENCE_CACHE:-.cache/picorg/face-embeddings-insightface-reference.json}\"; OUT=\"${INSIGHTFACE_CLUSTER_OUTPUT:-.cache/picorg/insightface-reference-clusters-identity.json}\"; if [[ ! -s \"$CACHE\" ]]; then echo \"ERROR: InsightFace reference cache not found: $CACHE; run option 7 first\"; exit 2; fi; .venv/bin/python cluster_insightface_reference_cache.py --cache \"$CACHE\" --output \"$OUT\" --threshold \"${INSIGHTFACE_CLUSTER_THRESHOLD:-1.164051}\" --max-representatives \"${INSIGHTFACE_MAX_REPRESENTATIVES:-5}\"; echo \"identity-isolated reference clusters: $OUT\""]], False),
    ("e", "Online evidence mock (no upload)", [["bash", "-lc", "set -Eeuo pipefail; AUDIT=\"${AUDIT:-$(find .cache/picorg/audits -maxdepth 1 -type f -name '20*.json' ! -name '*.preflight.json' ! -name '*.face-clusters.json' ! -name '*.reconciled*.json' ! -name '*.run-manifest.json' ! -name '*.identity-candidates.json' -printf '%T@ %p\\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p')}\"; if [[ -z \"$AUDIT\" || ! -s \"$AUDIT\" ]]; then echo 'ERROR: no primary audit found; run option 1 or 2 first'; exit 2; fi; QUEUE=\"${ONLINE_EVIDENCE_QUEUE:-.cache/picorg/reverse-search-queue.json}\"; OUT=\"${ONLINE_EVIDENCE_OUTPUT:-.cache/picorg/online-evidence.json}\"; .venv/bin/python reverse_search_queue.py \"$AUDIT\" --output \"$QUEUE\" --limit \"${ONLINE_EVIDENCE_LIMIT:-100}\" --per-gallery \"${ONLINE_EVIDENCE_PER_GALLERY:-3}\"; .venv/bin/python online_evidence.py \"$QUEUE\" --provider mock --output \"$OUT\"; echo \"online evidence report (no network): $OUT\""]], False),
    ("a", "Agent quality triage (report-only)", [["./run_agent_review.sh"]], False),
    ("h", "Help and command details", [["./run_picorg.sh", "--help"]], False),
]


class App:
    def __init__(self) -> None:
        self.lines: list[str] = ["Ready. Choose an action on the left."]
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.worker: threading.Thread | None = None
        self.confirmation: tuple[str, list[list[str]], bool] | None = None

    def log(self, line: str) -> None:
        self.lines.append(line.rstrip())
        self.lines = self.lines[-2000:]

    def start(self, commands: list[list[str]], label: str) -> None:
        if self.worker and self.worker.is_alive():
            self.log("An operation is already running.")
            return
        self.log(f"=== {label} ===")
        self.worker = threading.Thread(target=self.run_commands, args=(commands,), daemon=True)
        self.worker.start()

    def run_commands(self, commands: list[list[str]]) -> None:
        for command in commands:
            self.events.put(("log", "$ " + " ".join(command)))
            try:
                self.process = subprocess.Popen(
                    command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=os.environ.copy(), start_new_session=True,
                )
                if self.process.stdout is None:
                    raise RuntimeError("failed to capture operation output")
                for line in self.process.stdout:
                    self.events.put(("log", line))
                code = self.process.wait()
            except Exception as exc:  # UI must remain usable if a command fails.
                self.events.put(("log", f"ERROR: {exc}"))
                code = 1
            finally:
                self.process = None
            self.events.put(("log", f"command exit: {code}"))
            if code:
                self.events.put(("done", code))
                return
        self.events.put(("done", 0))

    def stop_current(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.log("Stop signal sent to the active operation tree.")
            except ProcessLookupError:
                self.log("Operation already exited.")
        else:
            self.log("No PicOrg operation is running.")

    def stop_face_rebuild(self) -> None:
        try:
            result = subprocess.run(["pgrep", "-f", "[r]ebuild_face_database.py"], text=True, capture_output=True, check=False)
            pids = [int(item) for item in result.stdout.split() if item.isdigit()]
            if not pids:
                self.log("No face database rebuild found.")
                return
            for pid in pids:
                os.kill(pid, signal.SIGTERM)
            self.log("Stop signal sent to face rebuild PID(s): " + ", ".join(map(str, pids)))
        except Exception as exc:
            self.log(f"ERROR stopping face rebuild: {exc}")


def draw(stdscr: curses.window, app: App, selected: int, prompt: str = "", log_scroll: int = 0, hscroll: int = 0) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    split = min(36, max(26, width // 3))
    try:
        stdscr.vline(0, split, curses.ACS_VLINE, max(0, height - 1))
        stdscr.addnstr(0, 1, "PicOrg", split - 3, curses.A_BOLD)
        stdscr.addnstr(1, 1, "Actions", split - 3, curses.A_UNDERLINE)
        for index, (key, label, _, _) in enumerate(MENU, start=2):
            attr = curses.A_REVERSE if index - 2 == selected else curses.A_NORMAL
            stdscr.addnstr(index, 1, f" {key}  {label}", split - 2, attr)
        status = "RUNNING" if app.worker and app.worker.is_alive() else "IDLE"
        stdscr.addnstr(height - 2, 1, status, split - 2, curses.A_BOLD)
        stdscr.addnstr(height - 1, 1, "q quit  x stop", split - 2)
        stdscr.addnstr(0, split + 2, "Process log", max(1, width - split - 4), curses.A_BOLD)
        log_height = max(1, height - 3)
        end = max(0, len(app.lines) - log_scroll)
        start = max(0, end - log_height)
        visible = app.lines[start:end]
        for row, line in enumerate(visible, start=1):
            stdscr.addnstr(row, split + 2, line[hscroll:], max(1, width - split - 4))
        if prompt:
            stdscr.addnstr(height - 1, split + 2, prompt, max(1, width - split - 4), curses.A_REVERSE)
        else:
            hint = f"PgUp/PgDn lines  Left/Right chars  offset {log_scroll},{hscroll}"
            stdscr.addnstr(height - 1, split + 2, hint, max(1, width - split - 4))
    except curses.error:
        pass
    stdscr.refresh()


def main(stdscr: curses.window) -> int:
    curses.curs_set(0)
    stdscr.timeout(100)
    app = App()
    selected = 0
    prompt = ""
    log_scroll = 0
    hscroll = 0

    def activate(index: int) -> None:
        nonlocal prompt
        key_value, label, commands, dangerous = MENU[index]
        if key_value == "6":
            prompt = "Stop active face rebuild? [y/N]"
            app.confirmation = (label, [], True)
        elif dangerous:
            prompt = f"Run {label}? Review the command log first. [y/N]"
            app.confirmation = (label, commands or [], True)
        else:
            app.start(commands or [], label)

    while True:
        while True:
            try:
                kind, value = app.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                app.log(str(value))
            elif kind == "done":
                app.log(f"operation complete (exit {value})")
        draw(stdscr, app, selected, prompt, log_scroll, hscroll)
        key = stdscr.getch()
        if key == -1:
            continue
        if prompt:
            if key in (ord("y"), ord("Y")):
                label, commands, _ = app.confirmation  # type: ignore[misc]
                prompt = ""
                app.confirmation = None
                if label == "Stop face rebuild":
                    app.stop_face_rebuild()
                else:
                    app.start(commands, label)
            elif key in (ord("n"), ord("N"), 27):
                app.log("Cancelled.")
                prompt = ""
                app.confirmation = None
            continue
        if key in (ord("q"), ord("Q")):
            if app.worker and app.worker.is_alive():
                app.log("Operation still running; press x to stop it first.")
            else:
                return 0
        elif key == ord("0"):
            if app.worker and app.worker.is_alive():
                app.log("Operation still running; press x to stop it first.")
            else:
                return 0
        elif key in (ord("x"), ord("X")):
            app.stop_current()
        elif key in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(MENU)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(MENU)
        elif key in (curses.KEY_PPAGE,):
            log_scroll = min(max(0, len(app.lines) - 1), log_scroll + max(1, stdscr.getmaxyx()[0] - 4))
        elif key in (curses.KEY_NPAGE,):
            log_scroll = max(0, log_scroll - max(1, stdscr.getmaxyx()[0] - 4))
        elif key in (curses.KEY_LEFT, ord("h")):
            hscroll = max(0, hscroll - 8)
        elif key in (curses.KEY_RIGHT, ord("l")):
            hscroll += 8
        elif key in (curses.KEY_ENTER, 10, 13):
            activate(selected)
        elif ord("1") <= key <= ord("9"):
            selected = key - ord("1")
            activate(selected)
        elif 0 <= key < 256 and chr(key).lower() in {
            menu_key.lower() for menu_key, _, _, _ in MENU if menu_key.isalpha()
        }:
            requested = chr(key).lower()
            for index, (menu_key, _, _, _) in enumerate(MENU):
                if menu_key == requested:
                    selected = index
                    activate(index)
                    break


if __name__ == "__main__":
    curses.wrapper(main)
