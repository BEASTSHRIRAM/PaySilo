"""Silo chat: the agentic assistant backend.

The chat model only ever *reads and proposes* through tools. Org context
is injected server-side from the authenticated session — never taken from
model input — and there is deliberately no tool that can approve a run or
move money: approval stays a TAP-signed human decision on the run itself.
"""
