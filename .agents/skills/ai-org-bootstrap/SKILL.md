---
name: ai-org-bootstrap
description: Run the Antigravity-native autonomous build pipeline to achieve a software development goal using speculative parallel execution.
---

# Custom Skill: AI Org Bootstrap

You are equipped with the **AI Org Bootstrap** skill. This skill allows you to invoke the Antigravity-native autonomous build pipeline to achieve complex software development goals in the workspace.

## Objective

When the user requests a software development goal (e.g., "Implement a new feature", "Fix a bug", "Refactor a module"), you must delegate the execution to the deterministic, speculative parallel build pipeline rather than trying to edit the files manually yourself.

## Execution Protocol

1. **Verify Workspace**: Ensure you are in the target repository workspace.
2. **Retrieve API Credentials**:
   - The autonomous pipeline runs as a Python script and requires either a `GEMINI_API_KEY` environment variable or Vertex AI credentials.
   - If the user has not set `GEMINI_API_KEY` in their environment, remind them that they can obtain a free API key from Google AI Studio (aistudio.google.com) or run `gcloud auth application-default login` if they prefer Vertex AI.
3. **Invoke the Pipeline**:
   - Run the Python controller script using your `run_command` tool.
   - Pass the user's goal as the `--goal` argument.
   - Specify the target workspace path as the `--repo` argument.

   Example command:
   ```sh
   python3 scripts/controller_goal.py --repo . --goal "Fix the subtraction bug in math_utils.py so that test_math.py passes"
   ```

4. **Monitor and Report**:
   - The pipeline will output a stream of events to `.agent-runs/<goal_id>_stream.jsonl`.
   - Monitor the command execution and read the stream log to keep the user informed of the parallel task execution, local CEGAR repairs, and final integration status.
   - Once the pipeline completes successfully, report the results and point the user to the merged changes or the created PR.
