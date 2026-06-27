import os
import sys
import json
import pathlib
import subprocess
import time
import threading
from typing import List, Dict, Any, Union, Optional

# Thread-safe lock for appending to stream log
_log_lock = threading.Lock()

# Define the absolute safety boundary
SAFE_BASE_DIR = pathlib.Path("/home/terum").resolve()


def append_stream_log(log_path: Union[str, pathlib.Path], event: Dict[str, Any]) -> None:
    """
    Appends a JSON-serialized event followed by a newline into the file at log_path.
    Guarantees parent directories exist and uses UTF-8 encoding.
    """
    path = pathlib.Path(log_path).resolve()

    # Enforce path safety for the log path
    if not is_within_directory(path, SAFE_BASE_DIR):
        raise PermissionError(f"Log path {path} is outside the allowed directory: {SAFE_BASE_DIR}")

    path.parent.mkdir(parents=True, exist_ok=True)

    event_str = json.dumps(event, ensure_ascii=False) + "\n"
    with _log_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(event_str)


def is_within_directory(path: Union[str, pathlib.Path], directory: Union[str, pathlib.Path]) -> bool:
    """
    Returns True if the resolved path is located within the resolved directory.
    """
    try:
        resolved_path = pathlib.Path(path).resolve()
        resolved_dir = pathlib.Path(directory).resolve()
        resolved_path.relative_to(resolved_dir)
        return True
    except ValueError:
        return False


def resolve_and_verify_path(path: Union[str, pathlib.Path], base_dir: Union[str, pathlib.Path]) -> pathlib.Path:
    """
    Resolves both paths and checks if the path is within base_dir.
    Raises PermissionError if the path escapes base_dir, otherwise returns the resolved path.
    """
    resolved_path = pathlib.Path(path).resolve()
    resolved_base = pathlib.Path(base_dir).resolve()

    if not is_within_directory(resolved_path, resolved_base):
        raise PermissionError(
            f"Access denied: Path {resolved_path} is outside the allowed directory {resolved_base}"
        )
    return resolved_path


def run_git(
    git_args: List[str],
    cwd: Union[str, pathlib.Path],
    check: bool = True,
    log_path: Optional[Union[str, pathlib.Path]] = None,
    role: Optional[str] = None
) -> subprocess.CompletedProcess:
    """
    Executes a git command safely using subprocess.run.

    Args:
        git_args: List of command arguments to append to 'git' (e.g. ['status', '--porcelain']).
        cwd: Current working directory, must be within SAFE_BASE_DIR.
        check: If True, raises subprocess.CalledProcessError on non-zero exit code.
        log_path: Optional path to append stream logs of the command execution.
        role: Optional role name to include in the stream logs.

    Returns:
        subprocess.CompletedProcess containing returncode, stdout, and stderr.
    """
    resolved_cwd = resolve_and_verify_path(cwd, SAFE_BASE_DIR)

    # Construct the full command safely (prevent shell injection)
    cmd = ["git"] + git_args

    start_time = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(resolved_cwd),
            capture_output=True,
            text=True,
            check=False
        )
        duration = time.time() - start_time
    except Exception as e:
        if log_path:
            error_event = {
                "type": "git_error",
                "role": role or "system",
                "command": cmd,
                "error": str(e),
                "timestamp": time.time()
            }
            try:
                append_stream_log(log_path, error_event)
            except Exception:
                pass
        raise e

    if log_path:
        log_event = {
            "type": "git_command",
            "role": role or "system",
            "command": cmd,
            "cwd": str(resolved_cwd),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": duration,
            "timestamp": time.time()
        }
        try:
            append_stream_log(log_path, log_event)
        except Exception:
            pass

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            returncode=result.returncode,
            cmd=cmd,
            output=result.stdout,
            stderr=result.stderr
        )

    return result


def git_status(cwd: Union[str, pathlib.Path], log_path: Optional[Union[str, pathlib.Path]] = None) -> List[Dict[str, str]]:
    """
    Runs git status --porcelain -uall and returns parsed modified/untracked files.
    """
    res = run_git(["status", "--porcelain", "-uall"], cwd=cwd, log_path=log_path)
    files = []
    for line in res.stdout.splitlines():
        if len(line) > 3:
            status = line[:2]
            file_path = line[3:].strip()
            files.append({"status": status, "path": file_path})
    return files


def git_add(cwd: Union[str, pathlib.Path], paths: List[str], log_path: Optional[Union[str, pathlib.Path]] = None) -> None:
    """
    Stages files to the git index.
    """
    resolved_cwd = pathlib.Path(cwd).resolve()
    for p in paths:
        resolve_and_verify_path(resolved_cwd / p, SAFE_BASE_DIR)
    run_git(["add"] + paths, cwd=cwd, log_path=log_path)


def git_commit(cwd: Union[str, pathlib.Path], message: str, log_path: Optional[Union[str, pathlib.Path]] = None) -> None:
    """
    Commits staged changes.
    """
    run_git(["commit", "-m", message], cwd=cwd, log_path=log_path)


def git_checkout(
    cwd: Union[str, pathlib.Path],
    ref: str,
    paths: Optional[List[str]] = None,
    log_path: Optional[Union[str, pathlib.Path]] = None
) -> None:
    """
    Runs git checkout.
    If paths are specified, checks out those specific paths from the ref.
    """
    args = ["checkout", ref]
    if paths:
        resolved_cwd = pathlib.Path(cwd).resolve()
        for p in paths:
            resolve_and_verify_path(resolved_cwd / p, SAFE_BASE_DIR)
        args += ["--"] + paths

    run_git(args, cwd=cwd, log_path=log_path)


def git_worktree_add(
    cwd: Union[str, pathlib.Path],
    path: Union[str, pathlib.Path],
    branch: str,
    commit_ish: Optional[str] = None,
    log_path: Optional[Union[str, pathlib.Path]] = None
) -> None:
    """
    Creates a new git worktree at the specified path.
    """
    resolved_path = resolve_and_verify_path(path, SAFE_BASE_DIR)

    args = ["worktree", "add", str(resolved_path), branch]
    if commit_ish:
        args.append(commit_ish)

    run_git(args, cwd=cwd, log_path=log_path)


def git_worktree_remove(
    cwd: Union[str, pathlib.Path],
    path: Union[str, pathlib.Path],
    force: bool = False,
    log_path: Optional[Union[str, pathlib.Path]] = None
) -> None:
    """
    Removes the git worktree at the specified path.
    """
    resolved_path = resolve_and_verify_path(path, SAFE_BASE_DIR)

    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(resolved_path))

    run_git(["worktree", "prune"], cwd=cwd, log_path=log_path)
    run_git(args, cwd=cwd, log_path=log_path)
