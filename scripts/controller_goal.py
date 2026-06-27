#!/usr/bin/env python3
import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
from typing import Any, Dict
import uuid

# Ensure the repository root is in the python path
repo_root = pathlib.Path(__file__).parent.parent.resolve()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from scripts.carrier import AntigravityCarrier
from scripts.goal_store import GoalStore
from scripts.pipeline import PipelineCoordinator

def deterministic_scan(repo_path: pathlib.Path) -> Dict[str, Any]:
    """
    Scans the repository deterministically, listing visible files and providing
    a concise tree structure and summary. Excludes common binary and dependency directories.
    """
    ignored_patterns = {
        ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
        ".agent-runs", "dist", "build", ".mypy_cache", ".tox", ".DS_Store"
    }
    
    files_list = []
    tree_lines = []
    
    def _walk(dir_path: pathlib.Path, prefix: str = ""):
        try:
            # Sort items deterministically
            items = sorted(list(dir_path.iterdir()), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        except FileNotFoundError:
            return

        # Filter ignored items
        visible_items = [item for item in items if item.name not in ignored_patterns]
        
        for i, item in enumerate(visible_items):
            is_last = (i == len(visible_items) - 1)
            connector = "└── " if is_last else "├── "
            
            # Record tree representation
            tree_lines.append(f"{prefix}{connector}{item.name}{'/' if item.is_dir() else ''}")
            
            if item.is_dir():
                next_prefix = prefix + ("    " if is_last else "│   ")
                _walk(item, next_prefix)
            else:
                rel_path = item.relative_to(repo_path)
                size = item.stat().st_size
                
                # Include snippet for small text files (under 20KB) to help IntentExtractor
                content_snippet = ""
                if size < 20000:
                    try:
                        content_snippet = item.read_text(encoding="utf-8", errors="ignore")[:2000]
                    except Exception:
                        pass
                
                files_list.append({
                    "path": str(rel_path),
                    "size": size,
                    "snippet": content_snippet
                })
                
    _walk(repo_path)
    return {
        "tree": "\n".join(tree_lines),
        "files": files_list
    }

def generate_goal_id(goal: str) -> str:
    """
    Generates a deterministic goal ID based on a hash of the goal string.
    """
    h = hashlib.sha256(goal.encode('utf-8')).hexdigest()[:12]
    return f"goal_{h}"

def extract_json(text: str) -> Dict[str, Any]:
    """
    Extracts and parses a JSON object from raw LLM text response.
    """
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or start > end:
        raise ValueError(f"No JSON object found in response: {text}")
    json_str = text[start:end+1]
    return json.loads(json_str)

async def run_architect_phase(repo_path: pathlib.Path, goal: str, goal_id: str, scan_data: Dict[str, Any], stream_log_path: pathlib.Path) -> Dict[str, Any]:
    """
    Invokes the Architect using the AntigravityCarrier to research, write specs/domain_specification.md,
    and generate the structured task plan edit_plan.json.
    """
    print("Invoking Architect to research specs and build task edit plan...")
    specs_dir = repo_path / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    
    carrier = AntigravityCarrier(workspace_root=str(repo_path), role_name="architect")
    
    prompt = f"""You are the Architect.
Your job is to read the high-level goal, perform the necessary specification research, write the spec file, and output the structured parallel task plan.

High-Level Goal:
{goal}

Repository Structure and Context:
Tree:
{scan_data['tree']}

Files Context (Snippets):
{json.dumps(scan_data['files'], indent=2)}

Please perform the following operations:
1. Research and write a comprehensive, code-ready spec file named 'specs/domain_specification.md' in the workspace. Detail all formulas, parameter tables, layouts, sound synth diagrams, and save JSON schemas. Enforce modern UX boundaries (no walkable menu grids, no static sidebars, floating HUDs, contextual popups, town hub overlays).
2. Generate the structured task plan. Break the goal down into one or more tasks.
   - For each task, define a list of `files_allowed_to_change` (only files that will actually be edited/created).
   - Define a list of `verification_commands` (compilers, linters, or test suites to run after editing to prove correctness).
   - Embed the gathered technical specifications, parameter constants, or math equations directly in the task description.

Please output a valid JSON object matching the following structure:
{{
  "goal_id": "{goal_id}",
  "goal": "{goal}",
  "tasks": [
    {{
      "task_id": "task_1",
      "description": "<detailed description of what to do, including exact specs and parameters for this task>",
      "files_allowed_to_change": ["relative/path/to/file1", "relative/path/to/file2"],
      "verification_commands": ["pytest tests/test_file1.py", "eslint relative/path/to/file2"]
    }}
  ]
}}

The output MUST be a valid JSON block. Do not include markdown code fences or any conversational prefix/suffix."""

    response_content = ""
    async for token in carrier.chat_stream(prompt, stream_log_path=str(stream_log_path)):
        sys.stdout.write(token)
        sys.stdout.flush()
        response_content += token
    print()
    
    try:
        edit_plan = extract_json(response_content)
        # Basic validation of required fields
        if "goal_id" not in edit_plan or "goal" not in edit_plan or "tasks" not in edit_plan:
            raise KeyError("Missing required keys in edit plan JSON")
        return edit_plan
    except Exception as e:
        print(f"Error parsing edit plan JSON: {e}", file=sys.stderr)
        print(f"Raw response: {response_content}", file=sys.stderr)
        raise e

async def main():
    parser = argparse.ArgumentParser(description="AI Org Bootstrap Antigravity Autonomous Builder")
    parser.add_argument("--repo", required=True, help="Path to the target repository workspace")
    parser.add_argument("--goal", required=True, help="High-level goal description")
    parser.add_argument("--goal-id", help="Optional custom goal ID to track or resume")
    parser.add_argument("--stream-log", help="Optional path to log the execution stream events")
    parser.add_argument("--mode", choices=["serial", "parallel"], default="parallel", help="Task execution mode (default: parallel)")
    args = parser.parse_args()
    
    repo_path = pathlib.Path(args.repo).resolve()
    if not repo_path.exists() or not repo_path.is_dir():
        print(f"Error: Target repo path does not exist or is not a directory: {repo_path}", file=sys.stderr)
        sys.exit(1)
        
    goal_id = args.goal_id or generate_goal_id(args.goal)
    
    # Resolve stream log path
    if args.stream_log:
        stream_log_path = pathlib.Path(args.stream_log).resolve()
    else:
        stream_log_path = repo_path / ".agent-runs" / f"{goal_id}_stream.jsonl"
    stream_log_path.parent.mkdir(parents=True, exist_ok=True)
    
    print(f"Starting execution for Goal ID: {goal_id}")
    print(f"Target Repository: {repo_path}")
    print(f"Stream Log: {stream_log_path}")
    
    # Initialize GoalStore
    print("Initializing Goal Store...")
    store = GoalStore(str(repo_path))
    
    # Try to load existing goal state
    state = None
    try:
        state = store.load(goal_id)
        if state:
            print(f"Loaded existing goal state for {goal_id}.")
    except Exception as e:
        print(f"No active goal state found for {goal_id}. Starting fresh.")
        
    if not state:
        # Scan target repository first
        print("Scanning repository...")
        scan_data = deterministic_scan(repo_path)
        
        # Generate Specifications and Edit Plan using the Architect
        edit_plan = await run_architect_phase(repo_path, args.goal, goal_id, scan_data, stream_log_path)
        
        state = {
            "goal_id": goal_id,
            "goal": args.goal,
            "edit_plan": edit_plan,
            "tasks_completed": [],
            "status": "in_progress"
        }
        
        # Save initial state in the GoalStore
        print("Saving initial edit plan to Goal Store...")
        store.save(goal_id, state, "Initialize goal edit plan")
        
    print("\n--- Current Edit Plan ---")
    print(json.dumps(state["edit_plan"], indent=2))
    print("-------------------------\n")
    
    # Initialize PipelineCoordinator
    print("Initializing Pipeline Coordinator...")
    pipeline = PipelineCoordinator(str(repo_path))
    
    tasks = state["edit_plan"].get("tasks", [])
    
    success = True
    if args.mode == "serial":
        print(f"\nExecuting {len(tasks)} tasks sequentially (Serial Execution)...")
        completed_tasks = []
        for i, t in enumerate(tasks):
            task_id = t.get("task_id")
            print(f"\n--- Running Task {i+1}/{len(tasks)}: {task_id} ---")
            
            res = await pipeline.run(
                role_name="developer",
                contract_prompt=f"Task Objective: {t.get('description')}\n\nPlease implement the changes requested in the objective.",
                allowed_paths=t.get("files_allowed_to_change", []),
                test_commands=t.get("verification_commands", []),
                stream_log_path=str(stream_log_path)
            )
            
            if not res["success"]:
                print(f"\nTask {task_id} failed: {res.get('error')}")
                success = False
                state["status"] = "failed"
                store.save(goal_id, state, f"Task {task_id} failed: {res.get('error')}")
                break
                
            print(f"Task {task_id} completed and merged successfully!")
            completed_tasks.append(task_id)
            state["tasks_completed"] = completed_tasks
            store.save(goal_id, state, f"Task {task_id} completed successfully")
            
        if success:
            print("\nAll tasks completed sequentially. Running final Specification Audit...")
            audit_attempts = 0
            max_audit_retries = 3
            while audit_attempts < max_audit_retries:
                audit_attempts += 1
                print(f"\n--- Running Specification Audit Attempt {audit_attempts}/{max_audit_retries} ---")
                audit_res = await pipeline._run_specification_audits(repo_path, goal_id, str(stream_log_path))
                
                if audit_res["success"]:
                    print("\nFinal Specification Audit passed successfully!")
                    break
                
                print(f"\nSpecification Audit failed. Issues found:\n{audit_res.get('message')}")
                
                if audit_attempts >= max_audit_retries:
                    print("\nReached max audit retries. Halting.")
                    success = False
                    state["status"] = "failed"
                    store.save(goal_id, state, f"Final Specification Audit failed: {audit_res.get('message')}")
                    break
                    
                print(f"\nLaunching Developer to repair integration and specification issues (CEGAR Loop)...")
                res = await pipeline.run(
                    role_name="developer",
                    contract_prompt=(
                        f"Your task is to fix the integration and specification issues reported by the QA auditor.\n\n"
                        f"### QA AUDIT FINDINGS:\n{audit_res.get('message')}\n\n"
                        f"Please modify index.html, style.css, game.js, and audio.js as needed to resolve these issues completely. "
                        f"Ensure all external scripts are properly imported (do NOT put all logic inside inline index.html scripts), RPG formulas match specifications, keybindings are corrected, and UI panels are properly collapsed inside the #menu-overlay."
                    ),
                    allowed_paths=["index.html", "style.css", "game.js", "audio.js"],
                    test_commands=["node --check game.js", "node --check audio.js"]
                )
                
                if not res["success"]:
                    print(f"\nIntegration repair task failed: {res.get('error')}")
                    success = False
                    state["status"] = "failed"
                    store.save(goal_id, state, f"Integration repair failed: {res.get('error')}")
                    break
                    
                print(f"Integration repair completed and merged successfully! Re-auditing...")
    else:
        # Format tasks for the PipelineCoordinator parallel execution API
        formatted_tasks = []
        for t in tasks:
            formatted_tasks.append({
                "id": t.get("task_id"),
                "role": "developer",
                "prompt": f"Task Objective: {t.get('description')}\n\nPlease implement the changes requested in the objective.",
                "allowed_paths": t.get("files_allowed_to_change", []),
                "test_commands": t.get("verification_commands", [])
            })
            
        print(f"\nExecuting {len(formatted_tasks)} tasks in parallel (Speculative Execution)...")
        res = await pipeline.execute_tasks_parallel(
            goal_id=goal_id,
            tasks=formatted_tasks,
            integration_test_commands=None,
            integration_role="verifier",
            stream_log_path=str(stream_log_path)
        )
        success = res["success"]
        if success:
            print("\nAll speculative tasks merged and verified successfully!")
            state["tasks_completed"] = [t.get("task_id") for t in tasks]
            state["status"] = "completed"
            store.save(goal_id, state, "Speculative parallel execution completed successfully")
        else:
            print(f"\nParallel execution failed: {res.get('message')}")
            state["status"] = "failed"
            store.save(goal_id, state, f"Parallel execution failed: {res.get('error')}")
                
    if success:
        print(f"\nAll tasks completed! Goal {goal_id} achieved successfully.")
        state["status"] = "completed"
        store.save(goal_id, state, "Complete goal")
    else:
        print(f"\nGoal {goal_id} execution halted due to task failure.")
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExecution interrupted by user.")
        sys.exit(130)
