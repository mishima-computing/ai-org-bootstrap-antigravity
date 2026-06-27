import os
import json
import pathlib
import subprocess
from typing import Dict, Any, List, Optional

class GoalStore:
    """
    Git-backed state persistence store for goals.
    Interacts with custom Git refs under refs/goals/<id>/wip and refs/goals/<id>/done.
    """
    def __init__(self, workspace_root: str | pathlib.Path):
        self.workspace_root = pathlib.Path(workspace_root).resolve()
        self._ensure_git_repo()

    def _ensure_git_repo(self):
        """
        Ensures the workspace is a valid Git repository.
        If not, initializes it and creates an initial commit if empty.
        """
        try:
            subprocess.run(["git", "--version"], capture_output=True, check=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            raise RuntimeError("Git CLI is not installed or not found in PATH.")

        git_dir = self.workspace_root / ".git"
        if not git_dir.exists():
            subprocess.run(["git", "init"], cwd=str(self.workspace_root), capture_output=True, text=True, check=True)

        # Check if HEAD is valid (if there are any commits)
        res = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=str(self.workspace_root), capture_output=True, text=True)
        if res.returncode != 0:
            # Create a local git user if not configured globally, to avoid commit errors
            user_name_check = subprocess.run(["git", "config", "user.name"], cwd=str(self.workspace_root), capture_output=True, text=True)
            user_email_check = subprocess.run(["git", "config", "user.email"], cwd=str(self.workspace_root), capture_output=True, text=True)
            
            if not user_name_check.stdout.strip():
                subprocess.run(["git", "config", "--local", "user.name", "Antigravity Agent"], cwd=str(self.workspace_root), capture_output=True)
            if not user_email_check.stdout.strip():
                subprocess.run(["git", "config", "--local", "user.email", "agent@antigravity.ai"], cwd=str(self.workspace_root), capture_output=True)
                
            # Create the initial empty commit to establish HEAD
            subprocess.run(["git", "commit", "--allow-empty", "-m", "Initial commit"], cwd=str(self.workspace_root), capture_output=True, text=True, check=True)

    def save(self, goal_id: str, state_dict: Dict[str, Any], commit_msg: str, done: bool = False) -> str:
        """
        Saves the state_dict and current worktree changes to a custom Git ref.
        
        Args:
            goal_id: Unique identifier for the goal.
            state_dict: JSON-serializable dictionary representing the goal state.
            commit_msg: Commit message describing the changes.
            done: If True, saves to refs/goals/<id>/done, otherwise refs/goals/<id>/wip.
            
        Returns:
            The commit hash of the saved state.
        """
        ref_name = f"refs/goals/{goal_id}/done" if done else f"refs/goals/{goal_id}/wip"
        
        goals_dir = self.workspace_root / ".goals"
        goals_dir.mkdir(parents=True, exist_ok=True)
        state_file = goals_dir / f"{goal_id}.json"
        
        existed_before = state_file.exists()
        orig_content = None
        if existed_before:
            orig_content = state_file.read_text(encoding="utf-8")
            
        # Write state dict to file
        state_file.write_text(json.dumps(state_dict, indent=2, ensure_ascii=False), encoding="utf-8")
        
        try:
            # 1. Write the current index to a tree (preserves staged state)
            res = subprocess.run(["git", "write-tree"], cwd=str(self.workspace_root), capture_output=True, text=True, check=True)
            orig_index_tree = res.stdout.strip()
            
            # 2. Stage all changes, including the state file
            subprocess.run(["git", "add", "-A"], cwd=str(self.workspace_root), capture_output=True, text=True, check=True)
            
            # 3. Write the WIP tree
            res = subprocess.run(["git", "write-tree"], cwd=str(self.workspace_root), capture_output=True, text=True, check=True)
            wip_tree = res.stdout.strip()
            
            # 4. Get the parent commit hash (HEAD)
            res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.workspace_root), capture_output=True, text=True)
            parent_hash = res.stdout.strip() if res.returncode == 0 else None
            
            # 5. Create the commit object
            commit_cmd = ["git", "commit-tree", wip_tree]
            if parent_hash:
                commit_cmd.extend(["-p", parent_hash])
            commit_cmd.extend(["-m", commit_msg])
            
            res = subprocess.run(commit_cmd, cwd=str(self.workspace_root), capture_output=True, text=True, check=True)
            commit_hash = res.stdout.strip()
            
            # 6. Update the custom ref
            subprocess.run(["git", "update-ref", ref_name, commit_hash], cwd=str(self.workspace_root), capture_output=True, text=True, check=True)
            
            # 7. Restore the original index
            subprocess.run(["git", "read-tree", orig_index_tree], cwd=str(self.workspace_root), capture_output=True, text=True, check=True)
            
            return commit_hash
            
        finally:
            # Clean up/restore the state file in the worktree
            if existed_before:
                if orig_content is not None:
                    state_file.write_text(orig_content, encoding="utf-8")
            else:
                if state_file.exists():
                    state_file.unlink()
                subprocess.run(["git", "rm", "--cached", "--ignore-unmatch", f".goals/{goal_id}.json"], cwd=str(self.workspace_root), capture_output=True)

    def load(self, goal_id: str) -> Dict[str, Any]:
        """
        Loads the state_dict from refs/goals/<id>/wip (falling back to refs/goals/<id>/done)
        and restores the WIP changes into the current worktree.
        
        Args:
            goal_id: Unique identifier for the goal.
            
        Returns:
            The state_dict that was saved.
        """
        ref_wip = f"refs/goals/{goal_id}/wip"
        ref_done = f"refs/goals/{goal_id}/done"
        
        ref_name = None
        commit_hash = None
        for ref in [ref_wip, ref_done]:
            res = subprocess.run(["git", "rev-parse", "--verify", ref], cwd=str(self.workspace_root), capture_output=True, text=True)
            if res.returncode == 0:
                ref_name = ref
                commit_hash = res.stdout.strip()
                break
                
        if not ref_name or not commit_hash:
            raise FileNotFoundError(f"No saved state found for goal ID '{goal_id}'")
            
        # 1. Read the state_dict from the commit
        show_cmd = ["git", "show", f"{ref_name}:.goals/{goal_id}.json"]
        res = subprocess.run(show_cmd, cwd=str(self.workspace_root), capture_output=True, text=True)
        if res.returncode != 0:
            raise ValueError(f"State file not found in ref '{ref_name}'")
            
        try:
            state_dict = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse state JSON from ref '{ref_name}': {e}")
            
        # 2. Restore the WIP changes into the current worktree
        try:
            # Attempt to cherry-pick the commit without committing
            subprocess.run(["git", "cherry-pick", "--no-commit", commit_hash], cwd=str(self.workspace_root), capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            # If cherry-pick failed, abort to clean up the merging state
            subprocess.run(["git", "cherry-pick", "--abort"], cwd=str(self.workspace_root), capture_output=True)
            raise RuntimeError(
                f"Failed to restore WIP changes for goal '{goal_id}' due to conflicts or errors. "
                f"Command output: {e.stderr.strip()}"
            )
            
        # 3. Clean up the state file from the worktree/index so it doesn't pollute the workspace
        state_file = self.workspace_root / ".goals" / f"{goal_id}.json"
        if state_file.exists():
            state_file.unlink()
        subprocess.run(["git", "rm", "--cached", "--ignore-unmatch", f".goals/{goal_id}.json"], cwd=str(self.workspace_root), capture_output=True)
        
        return state_dict

    def delete(self, goal_id: str):
        """
        Deletes both wip and done refs for the given goal ID.
        """
        for ref in [f"refs/goals/{goal_id}/wip", f"refs/goals/{goal_id}/done"]:
            subprocess.run(["git", "update-ref", "-d", ref], cwd=str(self.workspace_root), capture_output=True)

    def list_goals(self) -> List[str]:
        """
        Lists all goal IDs currently stored in the repository.
        """
        res = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/goals/"],
            cwd=str(self.workspace_root),
            capture_output=True,
            text=True
        )
        if res.returncode != 0:
            return []
            
        goals = set()
        for line in res.stdout.splitlines():
            parts = line.strip().split('/')
            if len(parts) >= 4:
                # e.g., refs/goals/123/wip -> parts = ['refs', 'goals', '123', 'wip']
                goals.add(parts[2])
        return sorted(list(goals))
