import os
import sys
import uuid
import shutil
import pathlib
import unittest
import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the project root is in sys.path
project_root = pathlib.Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from scripts.pipeline import PipelineCoordinator


class TestPipelineCoordinator(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        """Create a temporary directory for each test."""
        self.test_dir = pathlib.Path(f"/tmp/test_workspace_{uuid.uuid4().hex}")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.coordinator = PipelineCoordinator(workspace_root=str(self.test_dir))

    def tearDown(self):
        """Clean up the temporary directory."""
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    async def test_ensure_git_repo_initializes_and_commits(self):
        """Tests that _ensure_git_repo initializes a git repo and creates an initial commit."""
        await self.coordinator._ensure_git_repo()
        
        # Check that .git exists
        self.assertTrue((self.test_dir / ".git").exists())
        
        # Check that there is a commit
        res = await self.coordinator._run_git_cmd(["git", "rev-parse", "--verify", "HEAD"], self.test_dir)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(len(res.stdout.strip()), 40)  # Valid commit hash length

    async def test_create_and_cleanup_worktree(self):
        """Tests that a worktree can be created and cleaned up successfully."""
        await self.coordinator._ensure_git_repo()
        
        # Create worktree
        wt_path = await self.coordinator._create_worktree()
        self.assertTrue(wt_path.exists())
        self.assertEqual(wt_path.parent, self.test_dir / ".worktrees")
        
        # Check that git knows about the worktree
        res = await self.coordinator._run_git_cmd(["git", "worktree", "list"], self.test_dir)
        self.assertIn(str(wt_path), res.stdout)
        
        # Clean up worktree
        await self.coordinator._cleanup_worktree(wt_path)
        self.assertFalse(wt_path.exists())
        
        # Check that git no longer lists it
        res = await self.coordinator._run_git_cmd(["git", "worktree", "list"], self.test_dir)
        self.assertNotIn(str(wt_path), res.stdout)

    def test_resolve_paths(self):
        """Tests that paths are resolved correctly relative to the worktree directory."""
        wt_path = self.test_dir / ".worktrees" / "wt-test"
        
        # Relative path
        resolved = self.coordinator._resolve_paths(["src/main.py"], wt_path)
        self.assertEqual(resolved, [str(wt_path / "src/main.py")])
        
        # Absolute path under workspace root
        abs_path = self.test_dir / "src/utils.py"
        resolved = self.coordinator._resolve_paths([str(abs_path)], wt_path)
        self.assertEqual(resolved, [str(wt_path / "src/utils.py")])

    async def test_run_tests_success_and_failure(self):
        """Tests that _run_tests correctly executes commands and identifies passes/failures."""
        # Run passing command
        res = await self.coordinator._run_tests(["echo hello"], self.test_dir)
        self.assertTrue(res["success"])
        
        # Run failing command
        res = await self.coordinator._run_tests(["false"], self.test_dir)
        self.assertFalse(res["success"])
        self.assertEqual(len(res["failures"]), 1)
        self.assertEqual(res["failures"][0]["command"], "false")
        self.assertNotEqual(res["failures"][0]["exit_code"], 0)

    @patch("scripts.pipeline.AntigravityCarrier")
    async def test_pipeline_run_success_first_attempt(self, mock_carrier_class):
        """Tests a successful pipeline run on the first attempt."""
        def make_mock_carrier(*args, **kwargs):
            mock_instance = MagicMock()
            workspace_root = kwargs.get("workspace_root")
            
            async def mock_chat_stream(prompt, stream_log_path=None):
                wt_dir = pathlib.Path(workspace_root)
                allowed_file = wt_dir / "src" / "main.py"
                allowed_file.parent.mkdir(parents=True, exist_ok=True)
                allowed_file.write_text("print('hello world')", encoding="utf-8")
                yield "token"
                
            mock_instance.chat_stream = mock_chat_stream
            return mock_instance

        mock_carrier_class.side_effect = make_mock_carrier

        coordinator = PipelineCoordinator(workspace_root=str(self.test_dir), max_retries=3)
        
        result = await coordinator.run(
            role_name="implementer",
            contract_prompt="Implement hello world in src/main.py",
            allowed_paths=["src/main.py"],
            test_commands=["echo 'tests passed'"],
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["attempts"], 1)
        self.assertIn("commit_hash", result)
        
        main_file = self.test_dir / "src" / "main.py"
        self.assertTrue(main_file.exists())
        self.assertEqual(main_file.read_text(encoding="utf-8"), "print('hello world')")

    @patch("scripts.pipeline.AntigravityCarrier")
    async def test_pipeline_run_cegar_retry_success(self, mock_carrier_class):
        """Tests a pipeline run that fails initially but succeeds on retry (CEGAR loop)."""
        attempts_count = 0
        
        def make_mock_carrier(*args, **kwargs):
            mock_instance = MagicMock()
            workspace_root = kwargs.get("workspace_root")
            
            async def mock_chat_stream(prompt, stream_log_path=None):
                nonlocal attempts_count
                attempts_count += 1
                wt_dir = pathlib.Path(workspace_root)
                allowed_file = wt_dir / "src" / "main.py"
                allowed_file.parent.mkdir(parents=True, exist_ok=True)
                
                if attempts_count == 1:
                    allowed_file.write_text("broken_syntax = ", encoding="utf-8")
                else:
                    allowed_file.write_text("fixed_syntax = 10", encoding="utf-8")
                yield "token"
                
            mock_instance.chat_stream = mock_chat_stream
            return mock_instance

        mock_carrier_class.side_effect = make_mock_carrier

        coordinator = PipelineCoordinator(workspace_root=str(self.test_dir), max_retries=3)
        
        result = await coordinator.run(
            role_name="implementer",
            contract_prompt="Write valid python in src/main.py",
            allowed_paths=["src/main.py"],
            test_commands=["python3 -m py_compile src/main.py"],
        )
        
        self.assertTrue(result["success"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(attempts_count, 2)
        
        main_file = self.test_dir / "src" / "main.py"
        self.assertTrue(main_file.exists())
        self.assertEqual(main_file.read_text(encoding="utf-8"), "fixed_syntax = 10")

    @patch("scripts.pipeline.AntigravityCarrier")
    async def test_pipeline_run_failure_max_retries(self, mock_carrier_class):
        """Tests that the pipeline fails and does not merge if tests keep failing."""
        def make_mock_carrier(*args, **kwargs):
            mock_instance = MagicMock()
            workspace_root = kwargs.get("workspace_root")
            
            async def mock_chat_stream(prompt, stream_log_path=None):
                wt_dir = pathlib.Path(workspace_root)
                allowed_file = wt_dir / "src" / "main.py"
                allowed_file.parent.mkdir(parents=True, exist_ok=True)
                allowed_file.write_text("broken_syntax = ", encoding="utf-8")
                yield "token"
                
            mock_instance.chat_stream = mock_chat_stream
            return mock_instance

        mock_carrier_class.side_effect = make_mock_carrier

        coordinator = PipelineCoordinator(workspace_root=str(self.test_dir), max_retries=2)
        
        result = await coordinator.run(
            role_name="implementer",
            contract_prompt="Write valid python in src/main.py",
            allowed_paths=["src/main.py"],
            test_commands=["python3 -m py_compile src/main.py"],
        )
        
        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 2)
        self.assertIn("error_diagnostics", result)
        
        main_file = self.test_dir / "src" / "main.py"
        self.assertFalse(main_file.exists())

    @patch("scripts.pipeline.AntigravityCarrier")
    async def test_execute_tasks_parallel_success(self, mock_carrier_class):
        """Tests a successful Parallel Speculative Execution run with two concurrent tasks."""
        def make_mock_carrier(*args, **kwargs):
            mock_instance = MagicMock()
            workspace_root = kwargs.get("workspace_root")
            allowed_paths = kwargs.get("allowed_paths", [])
            
            async def mock_chat_stream(prompt, stream_log_path=None):
                wt_dir = pathlib.Path(workspace_root)
                # Identify which task this is based on allowed paths
                if any("task_a.py" in str(p) for p in allowed_paths):
                    f = wt_dir / "src" / "task_a.py"
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text("A = 'task A done'", encoding="utf-8")
                elif any("task_b.py" in str(p) for p in allowed_paths):
                    f = wt_dir / "src" / "task_b.py"
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text("B = 'task B done'", encoding="utf-8")
                yield "token"
                
            mock_instance.chat_stream = mock_chat_stream
            return mock_instance

        mock_carrier_class.side_effect = make_mock_carrier

        coordinator = PipelineCoordinator(workspace_root=str(self.test_dir), max_retries=3)
        
        tasks = [
            {
                "id": "task-a",
                "role": "implementer",
                "prompt": "Implement A in task_a.py",
                "allowed_paths": ["src/task_a.py"],
                "test_commands": ["echo 'A passed'"]
            },
            {
                "id": "task-b",
                "role": "implementer",
                "prompt": "Implement B in task_b.py",
                "allowed_paths": ["src/task_b.py"],
                "test_commands": ["echo 'B passed'"]
            }
        ]
        
        result = await coordinator.execute_tasks_parallel(
            goal_id="goal-success",
            tasks=tasks,
            integration_test_commands=["echo 'integration passed'"]
        )
        
        self.assertTrue(result["success"])
        self.assertIn("commit_hash", result)
        self.assertEqual(len(result["results"]), 2)
        
        # Verify that both files exist and are merged back into the main repository
        file_a = self.test_dir / "src" / "task_a.py"
        file_b = self.test_dir / "src" / "task_b.py"
        self.assertTrue(file_a.exists())
        self.assertTrue(file_b.exists())
        self.assertEqual(file_a.read_text(encoding="utf-8"), "A = 'task A done'")
        self.assertEqual(file_b.read_text(encoding="utf-8"), "B = 'task B done'")

    @patch("scripts.pipeline.AntigravityCarrier")
    async def test_execute_tasks_parallel_local_failure_abort(self, mock_carrier_class):
        """Tests that parallel execution aborts early and does not merge if a task fails local verification."""
        def make_mock_carrier(*args, **kwargs):
            mock_instance = MagicMock()
            workspace_root = kwargs.get("workspace_root")
            allowed_paths = kwargs.get("allowed_paths", [])
            
            async def mock_chat_stream(prompt, stream_log_path=None):
                wt_dir = pathlib.Path(workspace_root)
                if any("task_a.py" in str(p) for p in allowed_paths):
                    f = wt_dir / "src" / "task_a.py"
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text("A = 1", encoding="utf-8")
                yield "token"
                
            mock_instance.chat_stream = mock_chat_stream
            return mock_instance

        mock_carrier_class.side_effect = make_mock_carrier

        coordinator = PipelineCoordinator(workspace_root=str(self.test_dir), max_retries=2)
        
        tasks = [
            {
                "id": "task-a",
                "role": "implementer",
                "prompt": "Implement A",
                "allowed_paths": ["src/task_a.py"],
                "test_commands": ["echo 'A passed'"]
            },
            {
                "id": "task-b",
                "role": "implementer",
                "prompt": "Implement B (will fail)",
                "allowed_paths": ["src/task_b.py"],
                "test_commands": ["false"]
            }
        ]
        
        result = await coordinator.execute_tasks_parallel(
            goal_id="goal-abort",
            tasks=tasks
        )
        
        self.assertFalse(result["success"])
        self.assertEqual(result["phase"], "parallel_local")
        self.assertEqual(len(result["results"]), 2)
        
        # Verify that NO changes were merged back to the main repository
        file_a = self.test_dir / "src" / "task_a.py"
        self.assertFalse(file_a.exists())

    @patch("scripts.pipeline.AntigravityCarrier")
    async def test_execute_tasks_parallel_merge_conflict_resolved(self, mock_carrier_class):
        """Tests that merge conflicts between parallel tasks are resolved via the conflict CEGAR loop."""
        conflict_resolved_written = False
        
        def make_mock_carrier(*args, **kwargs):
            mock_instance = MagicMock()
            workspace_root = kwargs.get("workspace_root")
            allowed_paths = kwargs.get("allowed_paths", [])
            
            async def mock_chat_stream(prompt, stream_log_path=None):
                nonlocal conflict_resolved_written
                wt_dir = pathlib.Path(workspace_root)
                
                if "Merge Conflict Detected" not in prompt:
                    # Speculative task phases: edit the same line in same file
                    f = wt_dir / "src" / "shared.py"
                    f.parent.mkdir(parents=True, exist_ok=True)
                    if "task-a" in prompt or "Value A" in prompt:
                        f.write_text("SHARED_VAR = 'Value A'", encoding="utf-8")
                    else:
                        f.write_text("SHARED_VAR = 'Value B'", encoding="utf-8")
                else:
                    # Integration conflict resolution phase
                    f = wt_dir / "src" / "shared.py"
                    f.write_text("SHARED_VAR = 'Resolved value'", encoding="utf-8")
                    conflict_resolved_written = True
                yield "token"
                
            mock_instance.chat_stream = mock_chat_stream
            return mock_instance

        mock_carrier_class.side_effect = make_mock_carrier

        coordinator = PipelineCoordinator(workspace_root=str(self.test_dir), max_retries=3)
        await coordinator._ensure_git_repo()
        
        # Write a base file so that both tasks can modify it
        base_file = self.test_dir / "src" / "shared.py"
        base_file.parent.mkdir(parents=True, exist_ok=True)
        base_file.write_text("SHARED_VAR = 'Original'", encoding="utf-8")
        
        # Commit the base file
        await coordinator._run_git_cmd(["git", "add", "-A"], self.test_dir)
        await coordinator._run_git_cmd([
            "git", "-c", "user.name=Test", "-c", "user.email=test@test.com", "commit", "-m", "add shared.py"
        ], self.test_dir)

        tasks = [
            {
                "id": "task-a",
                "role": "implementer",
                "prompt": "Change shared.py to Value A",
                "allowed_paths": ["src/shared.py"],
                "test_commands": ["echo 'A passed'"]
            },
            {
                "id": "task-b",
                "role": "implementer",
                "prompt": "Change shared.py to Value B",
                "allowed_paths": ["src/shared.py"],
                "test_commands": ["echo 'B passed'"]
            }
        ]
        
        result = await coordinator.execute_tasks_parallel(
            goal_id="goal-conflict",
            tasks=tasks
        )
        
        self.assertTrue(result["success"])
        self.assertTrue(conflict_resolved_written)
        
        # Verify merged and resolved content in the main repository
        final_file = self.test_dir / "src" / "shared.py"
        self.assertTrue(final_file.exists())
        self.assertEqual(final_file.read_text(encoding="utf-8"), "SHARED_VAR = 'Resolved value'")

    @patch("scripts.pipeline.AntigravityCarrier")
    async def test_execute_tasks_parallel_integration_failure_resolved(self, mock_carrier_class):
        """Tests that integration test failures are repaired via the integration CEGAR loop."""
        integration_repaired = False
        
        def make_mock_carrier(*args, **kwargs):
            mock_instance = MagicMock()
            workspace_root = kwargs.get("workspace_root")
            allowed_paths = kwargs.get("allowed_paths", [])
            
            async def mock_chat_stream(prompt, stream_log_path=None):
                nonlocal integration_repaired
                wt_dir = pathlib.Path(workspace_root)
                
                if "Integration Test Failures" not in prompt:
                    if any("task_a.py" in str(p) for p in allowed_paths):
                        f = wt_dir / "src" / "task_a.py"
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_text("A = 10", encoding="utf-8")
                    elif any("task_b.py" in str(p) for p in allowed_paths):
                        f = wt_dir / "src" / "task_b.py"
                        f.parent.mkdir(parents=True, exist_ok=True)
                        f.write_text("B = 20", encoding="utf-8")
                else:
                    f = wt_dir / "src" / "integration_fix.py"
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text("FIXED = True", encoding="utf-8")
                    integration_repaired = True
                yield "token"
                
            mock_instance.chat_stream = mock_chat_stream
            return mock_instance

        mock_carrier_class.side_effect = make_mock_carrier

        coordinator = PipelineCoordinator(workspace_root=str(self.test_dir), max_retries=3)
        
        tasks = [
            {
                "id": "task-a",
                "role": "implementer",
                "prompt": "Implement A",
                "allowed_paths": ["src/task_a.py"],
                "test_commands": ["echo 'A passed'"]
            },
            {
                "id": "task-b",
                "role": "implementer",
                "prompt": "Implement B",
                "allowed_paths": ["src/task_b.py"],
                "test_commands": ["echo 'B passed'"]
            }
        ]
        
        result = await coordinator.execute_tasks_parallel(
            goal_id="goal-integration-cegar",
            tasks=tasks,
            integration_test_commands=["python3 -c 'import pathlib; import sys; sys.exit(0 if pathlib.Path(\"src/integration_fix.py\").exists() else 1)'"]
        )
        
        self.assertTrue(result["success"])
        self.assertTrue(integration_repaired)
        
        # Verify merged files in main repository
        self.assertTrue((self.test_dir / "src" / "task_a.py").exists())
        self.assertTrue((self.test_dir / "src" / "task_b.py").exists())
        self.assertTrue((self.test_dir / "src" / "integration_fix.py").exists())


if __name__ == "__main__":
    unittest.main()
