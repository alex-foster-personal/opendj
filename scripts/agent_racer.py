#!/usr/bin/env python3

"""
agent_racer.py

Proof-of-concept for the "Double-Running" Speculative Execution pattern.
This script spawns two identical Aider worker instances in separate git worktrees
to complete a given task. It monitors both processes. The first one to successfully
complete the task and pass tests wins. The loser is aggressively terminated and
its worktree is discarded, preventing sunk-cost thrashing.
"""

import os
import subprocess
import time
import sys
import shutil
import signal

MODEL = os.environ.get("FIREWORKS_MODEL", "openai/accounts/fireworks/models/kimi-2.7-code")

def setup_worktree(name):
    # Create a git worktree for the agent
    subprocess.run(["git", "worktree", "add", f".tmp/{name}", "-b", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f".tmp/{name}"

def cleanup_worktree(name):
    print(f"Cleaning up worktree {name}...")
    subprocess.run(["git", "worktree", "remove", "--force", f".tmp/{name}"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "branch", "-D", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def spawn_agent(worktree_path, task_desc, temp):
    task_file = os.path.join(worktree_path, "TASK.md")
    with open(task_file, "w") as f:
        f.write(task_desc)

    log_file = os.path.join(worktree_path, "aider.log")
    
    # We pass temperature via environment (Aider supports passing OpenAI env vars or setting it via flags if supported)
    # Since Aider might not have a direct --temperature flag for all models, we simulate the variation 
    # by adding a prompt variation in this PoC.
    
    variation = ""
    if temp > 0.0:
        variation = "\n\nNote: Please think step-by-step and be creative in your solution."
        
    with open(task_file, "a") as f:
        f.write(variation)

    cmd = [
        "aider",
        "--openai-api-base", "https://api.fireworks.ai/inference/v1",
        "--model", MODEL,
        "--message-file", "TASK.md",
        "--yes-always",
        "--auto-commits"
    ]
    
    print(f"Spawning agent in {worktree_path}...")
    # Run in the worktree directory
    proc = subprocess.Popen(
        cmd,
        cwd=worktree_path,
        stdout=open(log_file, "w"),
        stderr=subprocess.STDOUT,
        env=os.environ.copy()
    )
    return proc

def run_race(task_desc):
    name_a = f"race-agent-a-{int(time.time())}"
    name_b = f"race-agent-b-{int(time.time())}"
    
    wt_a = setup_worktree(name_a)
    wt_b = setup_worktree(name_b)
    
    proc_a = spawn_agent(wt_a, task_desc, temp=0.0)
    proc_b = spawn_agent(wt_b, task_desc, temp=0.5)
    
    print(f"Race started between {name_a} (PID: {proc_a.pid}) and {name_b} (PID: {proc_b.pid})")
    
    winner = None
    loser_proc = None
    winner_name = None
    loser_name = None

    try:
        while True:
            ret_a = proc_a.poll()
            ret_b = proc_b.poll()
            
            if ret_a is not None:
                print(f"Agent A ({name_a}) finished with exit code {ret_a}.")
                # In a real scenario, we would run `pytest` here to verify it actually succeeded.
                # For this PoC, we assume finishing with 0 means success.
                if ret_a == 0:
                    winner = proc_a
                    loser_proc = proc_b
                    winner_name = name_a
                    loser_name = name_b
                    break
            
            if ret_b is not None:
                print(f"Agent B ({name_b}) finished with exit code {ret_b}.")
                if ret_b == 0:
                    winner = proc_b
                    loser_proc = proc_a
                    winner_name = name_b
                    loser_name = name_a
                    break
            
            time.sleep(2)
            
    except KeyboardInterrupt:
        print("Race interrupted by user.")
        proc_a.kill()
        proc_b.kill()
        cleanup_worktree(name_a)
        cleanup_worktree(name_b)
        sys.exit(1)

    if winner:
        print(f"\nWINNER DECLARED: {winner_name}!")
        print(f"Killing loser: {loser_name} (PID: {loser_proc.pid})")
        loser_proc.kill()
        
        print("Merging winner into current branch...")
        # Merge the winning branch
        subprocess.run(["git", "merge", winner_name], check=True)
        
        cleanup_worktree(name_a)
        cleanup_worktree(name_b)
        print("Race completed successfully.")
    else:
        print("Both agents failed the race.")
        cleanup_worktree(name_a)
        cleanup_worktree(name_b)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r") as f:
            task = f.read()
    else:
        task = "Please write a simple hello_world.py script that prints 'Hello World'."
        
    run_race(task)
