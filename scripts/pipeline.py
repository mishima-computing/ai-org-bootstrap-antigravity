import asyncio
import os
import sys
import json
import uuid
import shutil
import logging
import pathlib
import argparse
import subprocess
from typing import List, Dict, Any, Optional

# Ensure parent directory is in sys.path so we can import from scripts/carrier.py
project_root = pathlib.Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from scripts.carrier import AntigravityCarrier
except ImportError:
    from carrier import AntigravityCarrier


class PipelineCoordinator:
    """
    Deterministic Pipeline Coordinator with Speculative Parallel Execution.
    Orchestrates isolated Git worktrees, invokes the Antigravity CodeGenerator carrier,
    runs verification/test suites, and executes Counterexample-Guided Abstraction Refinement (CEGAR)
    repair loops.
    Supports both serial execution and Parallel Speculative Execution with automated merge conflict
    resolution and integration-level CEGAR loops.
    """

    def __init__(self, workspace_root: str, max_retries: int = 3, logger: Optional[logging.Logger] = None):
        self.workspace_root = pathlib.Path(workspace_root).resolve()
        self.max_retries = max_retries
        self.logger = logger or logging.getLogger("PipelineCoordinator")
        if not logger:
            # Set up a clean default console logger if none is provided
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    async def _run_git_cmd(self, args: List[str], cwd: pathlib.Path) -> subprocess.CompletedProcess:
        """Runs a Git command asynchronously and captures its output."""
        self.logger.debug(f"Running command: {' '.join(args)} in {cwd}")
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr
        )

    async def _ensure_git_repo(self):
        """Ensures the workspace is a Git repository with at least one commit."""
        git_dir = self.workspace_root / ".git"
        if not git_dir.exists():
            self.logger.info(f"Initializing Git repository at {self.workspace_root}")
            res = await self._run_git_cmd(["git", "init"], self.workspace_root)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to initialize git repository: {res.stderr}")

        # Check if there is at least one commit
        res = await self._run_git_cmd(["git", "rev-parse", "--verify", "HEAD"], self.workspace_root)
        if res.returncode != 0:
            self.logger.info("No commits found in Git repository. Staging files and creating initial commit...")
            
            # Stage all current files to establish a clean base line
            await self._run_git_cmd(["git", "add", "-A"], self.workspace_root)
            
            commit_res = await self._run_git_cmd([
                "git",
                "-c", "user.name=Antigravity Coordinator",
                "-c", "user.email=coordinator@antigravity.ai",
                "commit",
                "--allow-empty",
                "-m", "Initial commit (automatically created by PipelineCoordinator)"
            ], self.workspace_root)
            if commit_res.returncode != 0:
                raise RuntimeError(f"Failed to create initial commit: {commit_res.stderr}")

    async def _create_worktree(self, commit_ish: Optional[str] = None) -> pathlib.Path:
        """Creates an isolated Git worktree detached from the specified commit-ish (defaults to HEAD)."""
        worktree_id = f"wt-{uuid.uuid4().hex}"
        worktree_path = self.workspace_root / ".worktrees" / worktree_id
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        self.logger.info(f"Creating isolated Git worktree at {worktree_path} (base: {commit_ish or 'HEAD'})")
        cmd = ["git", "worktree", "add", "--detach", str(worktree_path)]
        if commit_ish:
            cmd.append(commit_ish)
            
        res = await self._run_git_cmd(cmd, self.workspace_root)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create git worktree: {res.stderr}")
        return worktree_path

    async def _cleanup_worktree(self, worktree_path: pathlib.Path):
        """Cleans up and prunes the isolated Git worktree."""
        self.logger.info(f"Cleaning up Git worktree at {worktree_path}")
        
        # Remove worktree using git command
        res = await self._run_git_cmd(["git", "worktree", "remove", "--force", str(worktree_path)], self.workspace_root)
        if res.returncode != 0:
            self.logger.warning(f"Failed to remove git worktree via git command: {res.stderr.strip()}")
            
        # Prune worktree metadata inside Git
        await self._run_git_cmd(["git", "worktree", "prune"], self.workspace_root)
        
        # Clean up any leftover directory files if they exist
        if worktree_path.exists():
            try:
                shutil.rmtree(worktree_path, ignore_errors=True)
            except Exception as e:
                self.logger.warning(f"Failed to delete worktree directory {worktree_path}: {e}")

    def _resolve_paths(self, paths: List[str], base_dir: pathlib.Path) -> List[str]:
        """Resolves allowed paths relative to the temporary worktree directory."""
        resolved = []
        for p in paths:
            path_obj = pathlib.Path(p)
            if path_obj.is_absolute():
                try:
                    rel_path = path_obj.relative_to(self.workspace_root)
                except ValueError:
                    rel_path = path_obj.name
            else:
                rel_path = path_obj
            resolved.append(str((base_dir / rel_path).resolve()))
        return resolved

    async def _run_tests(self, test_commands: List[str], worktree_path: pathlib.Path) -> Dict[str, Any]:
        """Runs the specified test/verification commands in the worktree."""
        self.logger.info("Running verification/test suite...")
        failures = []

        for cmd in test_commands:
            self.logger.info(f"Running test command: {cmd}")
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                self.logger.warning(f"Test command failed: {cmd} (exit code: {proc.returncode})")
                failures.append({
                    "command": cmd,
                    "exit_code": proc.returncode,
                    "stdout": stdout,
                    "stderr": stderr
                })
            else:
                self.logger.info(f"Test command passed: {cmd}")

        if failures:
            return {"success": False, "failures": failures}
        return {"success": True}

    def _format_feedback(self, failures: List[Dict[str, Any]]) -> str:
        """Formats test failures into a clear, actionable feedback diagnostics string for the agent."""
        feedback = ["### Verification Failure Diagnostics\nThe previous implementation failed the verification tests."]
        for f in failures:
            feedback.append(f"\n#### Command Failed: `{f['command']}`")
            feedback.append(f"**Exit Code:** {f['exit_code']}")
            if f['stdout'].strip():
                feedback.append("**Stdout:**")
                feedback.append(f"```\n{f['stdout'].strip()}\n```")
            if f['stderr'].strip():
                feedback.append("**Stderr:**")
                feedback.append(f"```\n{f['stderr'].strip()}\n```")
            feedback.append("-" * 40)
        return "\n".join(feedback)

    def _format_exception_feedback(self, exc: Exception) -> str:
        """Formats unexpected exceptions or permission errors into agent feedback."""
        return (
            f"### Execution Error\n"
            f"An error occurred during the execution/scope validation of your task:\n"
            f"```\n{str(exc)}\n```\n"
            f"Please correct your approach, make sure to obey all constraints, and try again."
        )

    async def _commit_worktree(self, worktree_path: pathlib.Path, role_name: str, message_prefix: str = "Verify & merge") -> Optional[str]:
        """Stages and commits the successful changes in the worktree."""
        self.logger.info("Committing verified changes in the isolated worktree...")
        
        # Stage all changes
        await self._run_git_cmd(["git", "add", "-A"], worktree_path)
        
        commit_msg = f"{message_prefix}: {role_name} implementation"
        commit_res = await self._run_git_cmd([
            "git",
            "-c", "user.name=Antigravity Coordinator",
            "-c", "user.email=coordinator@antigravity.ai",
            "commit",
            "-m", commit_msg
        ], worktree_path)
        
        if commit_res.returncode != 0:
            self.logger.info("No changes to commit in the worktree (or commit was empty).")
            # Get current HEAD commit hash
            rev_res = await self._run_git_cmd(["git", "rev-parse", "HEAD"], worktree_path)
            return rev_res.stdout.strip() if rev_res.returncode == 0 else None
            
        rev_res = await self._run_git_cmd(["git", "rev-parse", "HEAD"], worktree_path)
        if rev_res.returncode == 0:
            commit_hash = rev_res.stdout.strip()
            self.logger.info(f"Worktree changes committed successfully. Hash: {commit_hash}")
            return commit_hash
        return None

    async def _merge_to_main(self, commit_hash: str):
        """Merges the verified commit hash back into the main repository."""
        self.logger.info(f"Merging verified commit {commit_hash} back into main repository...")
        res = await self._run_git_cmd([
            "git",
            "-c", "user.name=Antigravity Coordinator",
            "-c", "user.email=coordinator@antigravity.ai",
            "merge", commit_hash
        ], self.workspace_root)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to merge verified changes back to main repository: {res.stderr}")
        self.logger.info("Successfully merged changes back to main repository.")

    def _has_conflict_markers(self, directory: pathlib.Path) -> bool:
        """Checks recursively if any file in the directory contains Git conflict markers."""
        for root, dirs, files in os.walk(directory):
            if ".git" in dirs:
                dirs.remove(".git")
            for file in files:
                file_path = pathlib.Path(root) / file
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    if "<<<<<<<" in content or "=======" in content or ">>>>>>>" in content:
                        return True
                except Exception:
                    pass
        return False

    async def run(
        self,
        role_name: str,
        contract_prompt: str,
        allowed_paths: List[str],
        test_commands: List[str],
        stream_log_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Orchestrates the entire single-task pipeline:
        1. Ensures Git repo is ready.
        2. Creates an isolated worktree.
        3. Invokes the CodeGenerator via AntigravityCarrier.
        4. Runs test commands.
        5. Performs CEGAR repair loop up to max_retries if tests fail.
        6. Commits and merges changes back on success.
        """
        await self._ensure_git_repo()
        
        worktree_path = await self._create_worktree()
        success = False
        attempts = 0
        final_commit_hash = None
        error_diagnostics = None
        current_prompt = contract_prompt
        allowed_paths_in_worktree = self._resolve_paths(allowed_paths, worktree_path)

        try:
            while attempts < self.max_retries:
                attempts += 1
                self.logger.info(f"--- CEGAR Attempt {attempts}/{self.max_retries} ---")
                
                # Instantiate carrier pointing to the isolated worktree
                carrier = AntigravityCarrier(
                    workspace_root=str(worktree_path),
                    role_name=role_name,
                    allowed_paths=allowed_paths_in_worktree
                )

                self.logger.info("Invoking Antigravity Carrier chat...")
                tokens = []
                try:
                    async for token in carrier.chat_stream(current_prompt, stream_log_path=stream_log_path):
                        tokens.append(token)
                    response_text = "".join(tokens)
                except Exception as e:
                    self.logger.error(f"Carrier invocation failed with exception: {e}")
                    error_diagnostics = self._format_exception_feedback(e)
                    current_prompt = f"{contract_prompt}\n\n{error_diagnostics}"
                    continue

                # Run the verification/test suite
                test_result = await self._run_tests(test_commands, worktree_path)
                if test_result["success"]:
                    self.logger.info("Verification succeeded.")
                    success = True
                    break
                else:
                    self.logger.warning("Verification failed. Initiating CEGAR feedback loop...")
                    error_diagnostics = self._format_feedback(test_result["failures"])
                    current_prompt = f"{contract_prompt}\n\n{error_diagnostics}"

            if success:
                # Commit the changes in the worktree
                final_commit_hash = await self._commit_worktree(worktree_path, role_name)
            else:
                self.logger.error("Verification failed after maximum retries.")

        finally:
            # Always clean up the worktree completely
            await self._cleanup_worktree(worktree_path)

        if success and final_commit_hash:
            # Merge the changes back to the main repository
            await self._merge_to_main(final_commit_hash)
            return {
                "success": True,
                "commit_hash": final_commit_hash,
                "attempts": attempts,
                "message": f"Successfully implemented and verified contract after {attempts} attempts."
            }
        else:
            return {
                "success": False,
                "attempts": attempts,
                "error_diagnostics": error_diagnostics,
                "message": f"Pipeline failed to verify contract after {attempts} attempts."
            }

    async def execute_tasks_parallel(
        self,
        goal_id: str,
        tasks: List[Dict[str, Any]],
        integration_test_commands: Optional[List[str]] = None,
        integration_role: str = "implementer",
        stream_log_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Orchestrates Parallel Speculative Execution for multiple tasks:
        1. Runs all tasks in parallel, each in its own isolated worktree branched off the SAME base commit.
        2. Executes local CEGAR repair loops in parallel.
        3. Attempts to merge successful task commits in an isolated integration worktree.
        4. Performs integration-level CEGAR loops to resolve merge conflicts and semantic test failures.
        5. Merges the fully integrated and verified commit back to the main branch.
        """
        await self._ensure_git_repo()

        # Get base commit hash
        res = await self._run_git_cmd(["git", "rev-parse", "HEAD"], self.workspace_root)
        base_commit = res.stdout.strip()
        self.logger.info(f"--- Starting Parallel Speculative Execution (Goal: {goal_id}, Base Commit: {base_commit}) ---")

        # Define the isolated task execution runner
        async def run_task_isolated(task_info: Dict[str, Any]) -> Dict[str, Any]:
            task_id = task_info.get("id") or task_info.get("task_id") or f"task-{uuid.uuid4().hex[:8]}"
            role = task_info.get("role") or task_info.get("role_name") or "implementer"
            prompt = task_info.get("prompt") or task_info.get("contract_prompt")
            allowed_paths = task_info.get("allowed_paths") or []
            test_commands = task_info.get("test_commands") or task_info.get("verification_commands") or []

            self.logger.info(f"Starting task {task_id} in isolated worktree")
            
            wt_path = await self._create_worktree(base_commit)
            task_success = False
            task_attempts = 0
            task_commit_hash = None
            task_error = None
            task_current_prompt = prompt
            task_allowed_paths_in_wt = self._resolve_paths(allowed_paths, wt_path)

            try:
                while task_attempts < self.max_retries:
                    task_attempts += 1
                    self.logger.info(f"[{task_id}] Local Attempt {task_attempts}/{self.max_retries}")
                    
                    carrier = AntigravityCarrier(
                        workspace_root=str(wt_path),
                        role_name=role,
                        allowed_paths=task_allowed_paths_in_wt
                    )

                    tokens = []
                    try:
                        async for token in carrier.chat_stream(task_current_prompt, stream_log_path=stream_log_path):
                            tokens.append(token)
                    except Exception as e:
                        self.logger.error(f"[{task_id}] Carrier failed: {e}")
                        task_error = self._format_exception_feedback(e)
                        task_current_prompt = f"{prompt}\n\n{task_error}"
                        continue

                    # Run local verification
                    test_res = await self._run_tests(test_commands, wt_path)
                    if test_res["success"]:
                        self.logger.info(f"[{task_id}] Local verification passed")
                        task_success = True
                        break
                    else:
                        self.logger.warning(f"[{task_id}] Local verification failed")
                        task_error = self._format_feedback(test_res["failures"])
                        task_current_prompt = f"{prompt}\n\n{task_error}"

                if task_success:
                    task_commit_hash = await self._commit_worktree(wt_path, role, message_prefix=f"Task {task_id}")
                else:
                    self.logger.error(f"[{task_id}] Local verification failed after max retries")

            finally:
                await self._cleanup_worktree(wt_path)

            return {
                "task_id": task_id,
                "success": task_success,
                "commit_hash": task_commit_hash,
                "attempts": task_attempts,
                "error_diagnostics": task_error
            }

        # Run all tasks in parallel
        parallel_results = await asyncio.gather(*[run_task_isolated(t) for t in tasks])

        # Check if any task failed
        failed_tasks = [r for r in parallel_results if not r["success"]]
        if failed_tasks:
            self.logger.error("One or more parallel tasks failed local verification. Aborting speculative merge.")
            return {
                "success": False,
                "phase": "parallel_local",
                "results": parallel_results,
                "message": "Parallel speculative execution failed during local task verification."
            }

        self.logger.info("All parallel tasks passed local verification. Initiating integration phase...")

        # Create integration worktree
        integration_wt = await self._create_worktree(base_commit)
        integration_success = False
        integration_commit_hash = None
        integration_error = None
        
        try:
            # 1. Merge each task's speculative commit into the integration worktree
            for r in parallel_results:
                task_id = r["task_id"]
                commit_to_merge = r["commit_hash"]
                self.logger.info(f"[Integration] Merging speculative commit {commit_to_merge} (Task: {task_id})")
                
                merge_res = await self._run_git_cmd([
                    "git",
                    "-c", "user.name=Antigravity Coordinator",
                    "-c", "user.email=coordinator@antigravity.ai",
                    "merge", commit_to_merge
                ], integration_wt)
                
                self.logger.info(f"[Integration] Merge of {task_id} exit code: {merge_res.returncode}")
                self.logger.info(f"[Integration] Merge stdout: {merge_res.stdout.strip()}")
                self.logger.info(f"[Integration] Merge stderr: {merge_res.stderr.strip()}")
                
                if merge_res.returncode != 0:
                    self.logger.warning(f"[Integration] Merge conflict detected while merging task {task_id}. Initiating conflict resolution CEGAR...")
                    
                    # Integration Conflict CEGAR loop
                    conflict_resolved = False
                    conflict_attempts = 0
                    
                    while conflict_attempts < self.max_retries:
                        conflict_attempts += 1
                        self.logger.info(f"[Integration] Conflict Resolution Attempt {conflict_attempts}/{self.max_retries}")
                        
                        # Get status and diff with conflict markers
                        status_res = await self._run_git_cmd(["git", "status", "--porcelain"], integration_wt)
                        diff_res = await self._run_git_cmd(["git", "diff"], integration_wt)
                        
                        conflict_prompt = (
                            f"### Merge Conflict Detected\n"
                            f"While integrating parallel tasks for Goal '{goal_id}', a Git merge conflict occurred "
                            f"when merging Task '{task_id}'.\n\n"
                            f"**Conflicted Files:**\n```\n{status_res.stdout.strip()}\n```\n\n"
                            f"**Git Diff with Conflict Markers:**\n```diff\n{diff_res.stdout.strip()}\n```\n\n"
                            f"Please inspect the conflicted files, resolve all conflict markers (removing the <<<<<<<, =======, and >>>>>>> lines), "
                            f"and ensure the integrated code is correct, functional, and consistent.\n"
                        )
                        
                        carrier = AntigravityCarrier(
                            workspace_root=str(integration_wt),
                            role_name=integration_role
                        )
                        
                        tokens = []
                        try:
                            async for token in carrier.chat_stream(conflict_prompt, stream_log_path=stream_log_path):
                                tokens.append(token)
                        except Exception as e:
                            self.logger.error(f"[Integration] Conflict agent failed with exception: {e}")
                            continue

                        # Check if conflict markers are resolved
                        if self._has_conflict_markers(integration_wt):
                            self.logger.warning(f"[Integration] Conflict markers still present in the files after attempt {conflict_attempts}.")
                            continue

                        # Stage and commit the merge resolution
                        await self._run_git_cmd(["git", "add", "-A"], integration_wt)
                        commit_res = await self._run_git_cmd([
                            "git",
                            "-c", "user.name=Antigravity Coordinator",
                            "-c", "user.email=coordinator@antigravity.ai",
                            "commit",
                            "-m", f"Resolve merge conflict for task {task_id} during speculative integration"
                        ], integration_wt)
                        
                        if commit_res.returncode == 0:
                            self.logger.info(f"[Integration] Merge conflict resolved successfully for task {task_id}")
                            conflict_resolved = True
                            break
                        else:
                            self.logger.warning(f"[Integration] Commit failed after conflict resolution: {commit_res.stderr}")
                    
                    if not conflict_resolved:
                        raise RuntimeError(f"Failed to resolve merge conflict for task {task_id} after {self.max_retries} attempts.")

            # 2. Run integration tests (if any)
            if integration_test_commands:
                self.logger.info("[Integration] Running integration test suite...")
                test_res = await self._run_tests(integration_test_commands, integration_wt)
                
                if not test_res["success"]:
                    self.logger.warning("[Integration] Integration tests failed (semantic inconsistency). Initiating integration CEGAR loop...")
                    
                    integration_resolved = False
                    integration_attempts = 0
                    integration_current_error = self._format_feedback(test_res["failures"])
                    
                    while integration_attempts < self.max_retries:
                        integration_attempts += 1
                        self.logger.info(f"[Integration] Integration Repair Attempt {integration_attempts}/{self.max_retries}")
                        
                        integration_prompt = (
                            f"### Integration Test Failures\n"
                            f"The merged speculative changes failed the integration test suite for Goal '{goal_id}'.\n\n"
                            f"**Diagnostics:**\n{integration_current_error}\n\n"
                            f"Please analyze the semantic inconsistencies, edit the files to fix all bugs, and ensure the entire test suite passes.\n"
                        )
                        
                        carrier = AntigravityCarrier(
                            workspace_root=str(integration_wt),
                            role_name=integration_role
                        )
                        
                        tokens = []
                        try:
                            async for token in carrier.chat_stream(integration_prompt, stream_log_path=stream_log_path):
                                tokens.append(token)
                        except Exception as e:
                            self.logger.error(f"[Integration] Integration repair agent failed: {e}")
                            continue

                        # Re-run integration tests
                        test_res = await self._run_tests(integration_test_commands, integration_wt)
                        if test_res["success"]:
                            self.logger.info("[Integration] Integration tests passed successfully after repair.")
                            integration_resolved = True
                            
                            # Commit the integration repairs
                            await self._commit_worktree(integration_wt, integration_role, message_prefix="Integration repairs")
                            break
                        else:
                            integration_current_error = self._format_feedback(test_res["failures"])
                            
                    if not integration_resolved:
                        raise RuntimeError("Failed to resolve integration test failures (semantic inconsistencies) after maximum retries.")
                else:
                    self.logger.info("[Integration] Integration tests passed on first run.")
            else:
                self.logger.info("[Integration] No integration tests specified. Proceeding.")

            # Commit the final integrated state (if anything is unstaged)
            integration_commit_hash = await self._commit_worktree(integration_wt, integration_role, message_prefix="Final integration")
            integration_success = True

        except Exception as e:
            self.logger.error(f"[Integration] Integration failed with exception: {e}")
            integration_error = str(e)

        finally:
            await self._cleanup_worktree(integration_wt)

        if integration_success and integration_commit_hash:
            # Merge the final integrated commit back to the main repository
            await self._merge_to_main(integration_commit_hash)
            return {
                "success": True,
                "commit_hash": integration_commit_hash,
                "results": parallel_results,
                "message": f"Successfully integrated and verified all parallel tasks for Goal '{goal_id}'."
            }
        else:
            return {
                "success": False,
                "phase": "integration",
                "results": parallel_results,
                "error": integration_error,
                "message": f"Parallel integration failed: {integration_error}"
            }


async def main():
    parser = argparse.ArgumentParser(description="Deterministic Pipeline Coordinator CLI")
    parser.add_argument("--repo", required=True, help="Path to the target repository/workspace")
    parser.add_argument("--goal-id", default="goal-1", help="Goal identifier (for parallel execution)")
    parser.add_argument("--mode", choices=["serial", "parallel"], default="serial", help="Execution mode")
    parser.add_argument("--role", default="implementer", help="Specialized agent role name")
    parser.add_argument("--prompt", help="Contract prompt (serial mode only)")
    parser.add_argument("--allowed-paths", default="", help="Comma-separated allowed file paths (serial mode only)")
    parser.add_argument("--test-commands", default="", help="Comma-separated verification commands (serial mode only)")
    parser.add_argument("--max-retries", type=int, default=3, help="Maximum CEGAR retry attempts")
    parser.add_argument("--stream-log", help="Path to save streaming JSONL logs")
    
    # Arguments for parallel mode via JSON file
    parser.add_argument("--tasks-json", help="JSON file containing the list of tasks for parallel execution")
    parser.add_argument("--integration-tests", default="", help="Comma-separated integration test commands")

    args = parser.parse_args()

    coordinator = PipelineCoordinator(workspace_root=args.repo, max_retries=args.max_retries)

    if args.mode == "parallel":
        if not args.tasks_json:
            parser.error("--tasks-json is required in parallel mode")
        with open(args.tasks_json, "r", encoding="utf-8") as f:
            tasks = json.load(f)
        integration_tests = [c.strip() for c in args.integration_tests.split(",") if c.strip()]
        
        result = await coordinator.execute_tasks_parallel(
            goal_id=args.goal_id,
            tasks=tasks,
            integration_test_commands=integration_tests,
            integration_role=args.role,
            stream_log_path=args.stream_log
        )
    else:
        if not args.prompt:
            parser.error("--prompt is required in serial mode")
        allowed_paths = [p.strip() for p in args.allowed_paths.split(",") if p.strip()]
        test_commands = [c.strip() for c in args.test_commands.split(",") if c.strip()]
        
        result = await coordinator.run(
            role_name=args.role,
            contract_prompt=args.prompt,
            allowed_paths=allowed_paths,
            test_commands=test_commands,
            stream_log_path=args.stream_log
        )

    print(json.dumps(result, indent=2))
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(main())
