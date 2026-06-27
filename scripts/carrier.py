import asyncio
import os
import sys
import json
import pathlib
import subprocess
import shutil
from typing import AsyncIterator, List, Dict, Any
from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig

class AntigravityCarrier:
    """
    Antigravity-Native Carrier Harness.
    Spawns and manages specialized agents using the google-antigravity Python SDK,
    while enforcing strict deterministic scope boundaries and logging.
    """
    def __init__(self, workspace_root: str, role_name: str, allowed_paths: List[str] = None):
        self.workspace_root = pathlib.Path(workspace_root).resolve()
        self.role_name = role_name
        self.allowed_paths = [pathlib.Path(p).resolve() for p in (allowed_paths or [])]
        
        # Load carrier discipline and role prompt
        self.repo_root = pathlib.Path(__file__).parent.parent.resolve()
        self.discipline_path = self.repo_root / "bootstrap" / "carrier-discipline.md"
        self.role_path = self.repo_root / "roles" / f"{role_name}.md"
        
    def _load_system_instructions(self) -> str:
        discipline_content = ""
        if self.discipline_path.exists():
            discipline_content = self.discipline_path.read_text(encoding="utf-8")
            
        role_content = ""
        if self.role_path.exists():
            role_content = self.role_path.read_text(encoding="utf-8")
        else:
            # Fallback if specific role file is not found yet
            role_content = f"# Role: {self.role_name}\nExecute the given contract in accordance with your guidelines."
            
        return f"{discipline_content}\n\n{role_content}"
        
    async def chat_stream(self, prompt: str, stream_log_path: str = None) -> AsyncIterator[str]:
        """
        Executes the agent in a streaming fashion, capturing tokens, thoughts, and tool calls,
        and enforces post-run file scope boundaries.
        """
        system_instructions = self._load_system_instructions()
        
        # Determine if this role should be equipped with tools (search, terminal, write)
        is_tool_equipped = self.role_name in [
            "domain-specifier",
            "intent-extractor",
            "implementer",
            "spec-auditor",
            "aesthetic-reviewer",
            "functional-ci-action-writer",
            "security-ci-action-writer",
            "nonfunctional-ci-action-writer"
        ]
        is_write_role = is_tool_equipped
        
        # Check environment variables for Vertex AI configuration
        vertex = os.environ.get("USE_VERTEX", "").lower() in ("true", "1")
        project = os.environ.get("GCP_PROJECT")
        location = os.environ.get("GCP_LOCATION")
        
        if project and location:
            vertex = True
            
        # Build LocalAgentConfig kwargs
        config_kwargs = {
            "system_instructions": system_instructions
        }
        if is_tool_equipped:
            config_kwargs["capabilities"] = CapabilitiesConfig()
            
        if vertex:
            config_kwargs["vertex"] = True
            config_kwargs["project"] = project
            config_kwargs["location"] = location
            
        # Check if we should use agy CLI fallback
        gemini_key = os.environ.get("GEMINI_API_KEY")
        has_vertex = project and location
        
        if not gemini_key and not has_vertex:
            # Fall back to agy CLI!
            full_prompt = f"SYSTEM INSTRUCTIONS:\n{system_instructions}\n\nUSER PROMPT:\n{prompt}"
            cmd = [
                "/home/terum/.local/bin/agy",
                "--add-dir", str(self.workspace_root),
                "--dangerously-skip-permissions",
                "--print", full_prompt
            ]
            
            # Run the command asynchronously and stream stdout
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    token = line.decode("utf-8", errors="ignore")
                    # Log token
                    if stream_log_path:
                        self._append_stream_log(stream_log_path, {
                            "type": "token",
                            "role": self.role_name,
                            "content": token
                        })
                    yield token
            finally:
                if proc.returncode is None:
                    try:
                        proc.terminate()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
                if is_write_role:
                    self._enforce_scope()
            return

        config = LocalAgentConfig(**config_kwargs)
            
        # Spawn the agent using the async context manager
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            
            # Create background tasks to stream thoughts and tool calls to the log
            async def log_thoughts():
                try:
                    async for thought in response.thoughts:
                        event = {
                            "type": "thought",
                            "role": self.role_name,
                            "content": thought
                        }
                        if stream_log_path:
                            self._append_stream_log(stream_log_path, event)
                except AttributeError:
                    pass
                    
            async def log_tool_calls():
                try:
                    async for call in response.tool_calls:
                        event = {
                            "type": "tool_call",
                            "role": self.role_name,
                            "tool": call.name,
                            "args": call.args
                        }
                        if stream_log_path:
                            self._append_stream_log(stream_log_path, event)
                except AttributeError:
                    pass
                    
            log_task_1 = asyncio.create_task(log_thoughts())
            log_task_2 = asyncio.create_task(log_tool_calls())
            
            try:
                # Yield tokens to the caller in real-time
                async for token in response:
                    # Stream token events to the stream log if path is provided
                    if stream_log_path:
                        self._append_stream_log(stream_log_path, {
                            "type": "token",
                            "role": self.role_name,
                            "content": token
                        })
                    yield token
            finally:
                # Ensure logging tasks are cancelled and cleaned up
                log_task_1.cancel()
                log_task_2.cancel()
                await asyncio.gather(log_task_1, log_task_2, return_exceptions=True)
                
                # Deterministic Backbone: Enforce file changes scope boundaries post-run
                if is_write_role:
                    self._enforce_scope()
                    
    def _enforce_scope(self):
        """
        Inspects the workspace status using Git and reverts any changes made
        outside the allowed path list.
        """
        if not self.allowed_paths:
            return
            
        try:
            # Run git status to find modified or untracked files
            res = subprocess.run(
                ["git", "status", "--porcelain", "-uall"],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                check=True
            )
            
            modified_files = []
            for line in res.stdout.splitlines():
                if len(line) > 3:
                    file_path = line[3:].strip()
                    full_path = (self.workspace_root / file_path).resolve()
                    modified_files.append(full_path)
                    
            violations = []
            for mf in modified_files:
                is_allowed = False
                for ap in self.allowed_paths:
                    try:
                        mf.relative_to(ap)
                        is_allowed = True
                        break
                    except ValueError:
                        continue
                if not is_allowed:
                    violations.append(mf)
                    
            if violations:
                # Revert violations immediately
                for vf in violations:
                    rel_path = vf.relative_to(self.workspace_root)
                    tracked_check = subprocess.run(
                        ["git", "ls-files", "--error-unmatch", str(rel_path)],
                        cwd=str(self.workspace_root),
                        capture_output=True
                    )
                    if tracked_check.returncode == 0:
                        # Tracked file -> revert
                        subprocess.run(
                            ["git", "checkout", "HEAD", "--", str(rel_path)],
                            cwd=str(self.workspace_root)
                        )
                    else:
                        # Untracked file -> remove
                        if vf.is_file():
                            vf.unlink()
                        elif vf.is_dir():
                            shutil.rmtree(vf)
                            
                violation_names = [str(v.relative_to(self.workspace_root)) for v in violations]
                raise PermissionError(
                    f"Scope Violation: Agent modified files outside the allowed scope: {violation_names}. "
                    f"These changes have been automatically reverted by the deterministic carrier harness."
                )
        except Exception as e:
            if isinstance(e, PermissionError):
                raise e
            # Allow failure if workspace is not a git repository yet
            pass
            
    def _append_stream_log(self, log_path: str, event: Dict[str, Any]):
        p = pathlib.Path(log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
