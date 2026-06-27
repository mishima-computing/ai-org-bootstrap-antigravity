#!/usr/bin/env python3
import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
from typing import Any, Dict

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

async def generate_domain_specification(repo_path: pathlib.Path, goal: str, goal_id: str, stream_log_path: pathlib.Path):
    """
    Spawns a domain-specifier agent to research and write specs/domain_specification.md in the target workspace.
    """
    print("Invoking DomainSpecifier to research and write specs/domain_specification.md...")
    specs_dir = repo_path / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    
    carrier = AntigravityCarrier(workspace_root=str(repo_path), role_name="domain-specifier")
    prompt = f"""Please research the user's goal: '{goal}'.
Perform web searches using your tools to gather the exact rules, equations, layout parameters, data structures, and edge cases.
Write a comprehensive technical specification file named 'specs/domain_specification.md' in this workspace detailing your findings.
Do not use placeholder logic or mock data. Detail everything necessary for a perfect, authentic implementation."""
    
    response = ""
    async for token in carrier.chat_stream(prompt, stream_log_path=str(stream_log_path)):
        sys.stdout.write(token)
        sys.stdout.flush()
        response += token
    print("\nDomain Specification generated successfully.")

async def extract_edit_plan(repo_path: pathlib.Path, goal: str, goal_id: str, scan_data: Dict[str, Any], stream_log_path: pathlib.Path) -> Dict[str, Any]:
    """
    Invokes the IntentExtractor using the AntigravityCarrier to generate
    the structured edit plan matching the edit_plan schema.
    """
    carrier = AntigravityCarrier(workspace_root=str(repo_path), role_name="intent-extractor")
    
    prompt = f"""You are the IntentExtractor.
Your job is to read a high-level goal and a repository scan, and generate a precise, structured edit plan.

High-Level Goal:
{goal}

Repository Structure and Context:
Tree:
{scan_data['tree']}

Files Context (Snippets):
{json.dumps(scan_data['files'], indent=2)}

Please output a valid JSON object matching the following structure:
{{
  "goal_id": "{goal_id}",
  "goal": "{goal}",
  "tasks": [
    {{
      "task_id": "task_1",
      "description": "<detailed description of what to do>",
      "files_allowed_to_change": ["relative/path/to/file1", "relative/path/to/file2"],
      "verification_commands": ["pytest tests/test_file1.py", "eslint relative/path/to/file2"]
    }}
  ]
}}

Guidelines:
1. `files_allowed_to_change` lists ONLY the files that this task needs to create or modify. Do not include files that only need to be read.
2. `verification_commands` lists commands that can be run deterministically to verify the correctness of the change (e.g. tests, linters).
3. The output MUST be a valid JSON block. Do not include markdown code fences or any conversational prefix/suffix.
4. Research-First: You MUST run search queries or read local reference documentations to obtain precise mathematical formulas, exact data lists, API specifications, or configuration structures required for the goal before finalizing the task descriptions. Include these gathered specifications and parameter arrays directly inside the description of the relevant tasks so that the developer agent does not use placeholder values.
"""

    response_content = ""
    print("Streaming tokens from IntentExtractor...")
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
        # Generate Domain Specification first
        await generate_domain_specification(repo_path, args.goal, goal_id, stream_log_path)

        # Scan target repository
        print("Scanning repository...")
        scan_data = deterministic_scan(repo_path)
        
        # Generate edit plan using IntentExtractor
        print("Invoking IntentExtractor to build edit plan...")
        edit_plan = await extract_edit_plan(repo_path, args.goal, goal_id, scan_data, stream_log_path)
        
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
    
    # Format tasks for the PipelineCoordinator parallel execution API
    formatted_tasks = []
    for t in tasks:
        formatted_tasks.append({
            "id": t.get("task_id"),
            "role": "implementer",
            "prompt": f"Task Objective: {t.get('description')}\n\nPlease implement the changes requested in the objective.",
            "allowed_paths": t.get("files_allowed_to_change", []),
            "test_commands": t.get("verification_commands", [])
        })
        
    print(f"\nExecuting {len(formatted_tasks)} tasks in parallel (Speculative Execution)...")
    
    # Execute the speculative parallel pipeline
    res = await pipeline.execute_tasks_parallel(
        goal_id=goal_id,
        tasks=formatted_tasks,
        integration_test_commands=None,  # Integration-level tests can be added if defined
        integration_role="implementer",
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
