#!/usr/bin/env python3

"""
en_masse_runner.py

A background automation script for high-volume tasks (UI-testing, auto-hardening, code refactoring).
It reads from .planning/EXPLORATION_BACKLOG.md, checks out a new branch, and delegates the work
to a Fireworks AI model running in an Aider harness.

The Orchestrator agent (Claude Code / Sol) does not need to be active while this runs.
"""

import os
import subprocess
import time
import re
from datetime import datetime

BACKLOG_FILE = ".planning/EXPLORATION_BACKLOG.md"
MODEL = os.environ.get("FIREWORKS_MODEL", "openai/accounts/fireworks/models/kimi-2.7-code")

def get_next_task():
    if not os.path.exists(BACKLOG_FILE):
        return None, None

    with open(BACKLOG_FILE, 'r') as f:
        lines = f.readlines()

    task_name = None
    task_desc = []
    task_start_idx = -1
    task_end_idx = -1

    in_task = False
    for i, line in enumerate(lines):
        if line.startswith("## [TODO]"):
            if not in_task:
                task_name = line.replace("## [TODO]", "").strip()
                in_task = True
                task_start_idx = i
            else:
                task_end_idx = i
                break
        elif in_task:
            task_desc.append(line)

    if not task_name:
        return None, None

    if task_end_idx == -1:
        task_end_idx = len(lines)

    # Rewrite backlog to mark as in progress
    lines[task_start_idx] = f"## [IN PROGRESS] {task_name}\n"
    with open(BACKLOG_FILE, 'w') as f:
        f.writelines(lines)

    return task_name, "".join(task_desc).strip()

def mark_task_status(task_name, status):
    if not os.path.exists(BACKLOG_FILE):
        return

    with open(BACKLOG_FILE, 'r') as f:
        content = f.read()

    # Replace [IN PROGRESS] with the new status
    content = content.replace(f"## [IN PROGRESS] {task_name}", f"## [{status}] {task_name}")
    
    with open(BACKLOG_FILE, 'w') as f:
        f.write(content)

def run_fireworks_harness(task_name, task_desc):
    safe_name = re.sub(r'[^a-zA-Z0-9]+', '-', task_name.lower())
    branch_name = f"auto/{safe_name}-{int(time.time())}"

    print(f"\n--- Starting Task: {task_name} ---")
    print(f"Creating branch: {branch_name}")
    
    subprocess.run(["git", "checkout", "-b", branch_name], check=False)

    task_file_path = f".tmp/{safe_name}_TASK.md"
    os.makedirs(".tmp", exist_ok=True)
    with open(task_file_path, 'w') as f:
        f.write(f"# Task: {task_name}\n\n{task_desc}\n\nWhen done, run tests to verify.\n")

    log_file = f".tmp/{safe_name}_worker.log"
    print(f"Spawning Fireworks model {MODEL} via Aider...")
    print(f"Logging to: {log_file}")

    # The actual call to aider
    cmd = [
        "aider",
        "--openai-api-base", "https://api.fireworks.ai/inference/v1",
        "--model", MODEL,
        "--message-file", task_file_path,
        "--yes-always",
        "--auto-commits"
    ]

    try:
        with open(log_file, "w") as log:
            result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        
        if result.returncode == 0:
            print("Aider worker finished successfully.")
            
            # Optionally run a verification script here
            # e.g., result = subprocess.run(["npm", "test"])

            mark_task_status(task_name, "DONE")
            print(f"Task marked DONE. Branch {branch_name} is ready for review.")
        else:
            print(f"Aider worker failed with code {result.returncode}.")
            mark_task_status(task_name, "FAILED")
            
    except FileNotFoundError:
        print("ERROR: 'aider' command not found. Please install Aider (e.g. pip install aider-chat).")
        mark_task_status(task_name, "FAILED-NO-AIDER")
    
    # Return to previous branch
    subprocess.run(["git", "checkout", "-"], check=False)

def main():
    print("Fireworks En-Masse Runner Started.")
    print(f"Monitoring {BACKLOG_FILE} for [TODO] items...")
    
    if not os.environ.get("FIREWORKS_API_KEY"):
        print("WARNING: FIREWORKS_API_KEY is not set.")

    while True:
        task_name, task_desc = get_next_task()
        if task_name:
            run_fireworks_harness(task_name, task_desc)
        else:
            print("No [TODO] tasks found in backlog. Sleeping for 60s...")
            time.sleep(60)

if __name__ == "__main__":
    main()
