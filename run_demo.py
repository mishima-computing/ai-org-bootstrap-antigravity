#!/usr/bin/env python3
import os
import sys
import pathlib
import subprocess
import argparse

def setup_demo_workspace(repo_root: pathlib.Path) -> pathlib.Path:
    demo_dir = repo_root / "demo_workspace"
    print(f"[*] Setting up demo workspace at: {demo_dir}")
    
    # Clean up previous demo workspace if it exists
    if demo_dir.exists():
        import shutil
        shutil.rmtree(demo_dir, ignore_errors=True)
        
    demo_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Initialize Git in the demo workspace
    subprocess.run(["git", "init"], cwd=str(demo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Demo Developer"], cwd=str(demo_dir), capture_output=True)
    subprocess.run(["git", "config", "user.email", "demo@example.com"], cwd=str(demo_dir), capture_output=True)
    
    # 2. Create buggy code (math_utils.py)
    buggy_code = """# math_utils.py

def add(a, b):
    # Bug: subtraction instead of addition
    return a - b

def multiply(a, b):
    # Bug: addition instead of multiplication
    return a + b
"""
    (demo_dir / "math_utils.py").write_text(buggy_code, encoding="utf-8")
    
    # 3. Create unit tests (test_math.py) that will fail due to the bugs
    tests_code = """# test_math.py
import unittest
from math_utils import add, multiply

class TestMathOperations(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(add(10, 5), 15)

    def test_multiplication(self):
        self.assertEqual(multiply(4, 5), 20)
"""
    (demo_dir / "test_math.py").write_text(tests_code, encoding="utf-8")
    
    # 4. Stage and commit the buggy state
    subprocess.run(["git", "add", "-A"], cwd=str(demo_dir), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with buggy math utilities"], cwd=str(demo_dir), capture_output=True, check=True)
    
    print("[+] Demo workspace successfully initialized and committed.")
    return demo_dir

def main():
    repo_root = pathlib.Path(__file__).parent.resolve()
    
    print("=================================================================")
    print("       AI Org Bootstrap Antigravity - Speculative Demo Setup     ")
    print("=================================================================\n")
    print("This script prepares a local 'demo_workspace' containing buggy code")
    print("and failing tests, representing a typical software development task.")
    print("We will run the speculative parallel pipeline to fix both bugs simultaneously!\n")
    
    demo_dir = setup_demo_workspace(repo_root)
    
    print("\n[!] The demo workspace contains two bugs:")
    print("    1. add(a, b) returns a - b")
    print("    2. multiply(a, b) returns a + b")
    print("    Both tests in test_math.py are currently failing.\n")
    
    # Check if credentials are present in the environment
    gemini_key = os.environ.get("GEMINI_API_KEY")
    gcp_project = os.environ.get("GCP_PROJECT")
    gcp_location = os.environ.get("GCP_LOCATION")
    
    has_credentials = gemini_key or (gcp_project and gcp_location)
    
    if has_credentials:
        print("[*] Active credentials detected in your environment!")
        if gemini_key:
            print("    - Using Gemini Developer API Key.")
        else:
            print(f"    - Using Vertex AI (Project: {gcp_project}, Location: {gcp_location}).")
            
        print("\nDo you want to run the autonomous speculative pipeline live right now?")
        choice = input("Run live demo? (y/N): ").strip().lower()
        if choice == 'y':
            print("\n[*] Launching speculative parallel pipeline...")
            cmd = [
                sys.executable,
                str(repo_root / "scripts" / "controller_goal.py"),
                "--repo", str(demo_dir),
                "--goal", "Fix the bugs in math_utils.py so that both addition and multiplication tests in test_math.py pass successfully in parallel."
            ]
            # Execute and stream output
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
            proc.wait()
            
            if proc.returncode == 0:
                print("\n[+] DEMO SUCCESSFUL! The bugs were automatically fixed, verified, and merged in parallel.")
                # Show the git diff
                print("\n[*] Resulting Git Diff in demo_workspace:")
                diff_res = subprocess.run(["git", "diff", "HEAD~1"], cwd=str(demo_dir), capture_output=True, text=True)
                print(diff_res.stdout)
            else:
                print(f"\n[-] Demo run failed with exit code: {proc.returncode}")
            return
            
    # If no credentials or user chose not to run, show the commands
    print("\n[*] To run this speculative parallel execution demo, use one of the following commands:\n")
    print("--- Option A: Gemini Developer API (Google AI Studio) ---")
    print("  export GEMINI_API_KEY=\"your_api_key\"")
    print(f"  python3 scripts/controller_goal.py --repo {demo_dir.relative_to(repo_root)} --goal \"Fix the bugs in math_utils.py so that both addition and multiplication tests in test_math.py pass in parallel.\"")
    print("")
    print("--- Option B: Vertex AI (GCP Google Account Login) ---")
    print("  gcloud auth application-default login")
    print("  export GCP_PROJECT=\"your_project_id\" GCP_LOCATION=\"us-central1\"")
    print(f"  python3 scripts/controller_goal.py --repo {demo_dir.relative_to(repo_root)} --goal \"Fix the bugs in math_utils.py so that both addition and multiplication tests in test_math.py pass in parallel.\"")
    print("\nObserve the live speculative task execution, local CEGAR loops, and final integration in action!")
    print("=================================================================\n")

if __name__ == "__main__":
    main()
