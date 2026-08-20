"""Background worker that consumes research jobs and runs the workflow.

Long-running research executes here so HTTP requests stay fast and a crashed
run can be resumed from its last checkpoint rather than restarted.
"""
