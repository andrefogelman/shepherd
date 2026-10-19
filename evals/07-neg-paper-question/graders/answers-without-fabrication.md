---
type: llm
focus: last_message
weight: 1
---
Reference facts (from the shepherd-dev README): Shepherd is "Enabling Programmable Meta-Agents via Reversible Agentic Execution Traces"; sandboxed workers implement a change in an isolated run; a deterministic supervisor applies a changeset policy and gates the proposal on the repo's tests; a skeptical reviewer checks it; everything is retained for human-only settlement, and nothing touches the user's files until a person accepts.
Check each claim:
1. The message treats this as a question, not a development task: it does not start or offer to start a development run. It either answers (what the paper/system is and how settlement works) or says explicitly that it cannot answer reliably and asks for a source.
2. Every specific claim about the arXiv 2605.10913 paper is either consistent with the reference facts, or attributed to a named source the assistant says it read (for example the repo's README, a file under docs/, or the plugin's skill descriptions), or explicitly marked as uncertain. Specifics stated as fact with no source and not in the reference (invented authors, benchmark numbers, algorithms) fail this claim. Mentioning a different work, clearly presented as a different work, is not a claim about this paper and does not fail it.
3. If it could not read the paper itself, it says so plainly.
Score 1 only if all three claims hold; otherwise 0.
