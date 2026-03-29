from __future__ import annotations

import re

BUG_KEYWORDS = {
    "fix", "broken", "error", "crash", "bug", "issue", "wrong", "fails",
    "failure", "breaking", "broke", "fault", "defect", "regression",
}
FEATURE_KEYWORDS = {
    "add", "create", "implement", "new", "build", "support", "enable",
    "introduce", "develop", "integrate",
}
REDESIGN_KEYWORDS = {
    "refactor", "redesign", "rewrite", "rework", "improve", "restructure",
    "cleanup", "modernize", "optimize", "simplify", "migrate", "overhaul",
}
PLAN_KEYWORDS = {
    "plan", "analyze", "analyse", "evaluate", "assess", "investigate",
    "research", "explore", "review", "audit", "strategy", "proposal",
    "architecture", "design", "rfc", "spec", "blueprint",
}


def detect_mode(text: str) -> str:
    words = set(re.findall(r"\w+", text.lower()))
    if words & PLAN_KEYWORDS:
        return "plan"
    if words & BUG_KEYWORDS:
        return "bug"
    if words & REDESIGN_KEYWORDS:
        return "redesign"
    if words & FEATURE_KEYWORDS:
        return "feature"
    return "feature"


MODE_PROMPTS = {
    "bug": """\
You are fixing a bug. Follow this approach:

1. **Reproduce** — Understand the exact conditions that trigger the bug
2. **Root Cause** — Find the underlying cause, not just the symptom
3. **Minimal Fix** — Make the smallest change that correctly fixes the issue
4. **Regression Test** — If tests exist, add a test case that would have caught this bug
5. **Verify** — Confirm the fix works and doesn't break anything else

Do NOT refactor surrounding code. Do NOT add features. Fix the bug, nothing more.""",

    "feature": """\
You are implementing a new feature. Follow this approach:

1. **Understand** — Read existing code and patterns before writing anything
2. **Design** — Choose the simplest approach that fits the existing architecture
3. **Implement** — Write clean, idiomatic code that matches the project's style
4. **Test** — Add tests if the project has a test suite
5. **Document** — Update docs only if the project has existing documentation

Follow existing patterns. Don't over-engineer. Don't add configuration for things that don't need it.""",

    "redesign": """\
You are redesigning/refactoring existing code. Follow this approach:

1. **Understand** — Thoroughly read and understand the current implementation before changing anything
2. **Plan** — Design the target architecture; identify what changes and what stays
3. **Incremental** — Make changes incrementally, verifying each step
4. **Preserve Behavior** — Unless explicitly asked to change behavior, the refactored code must do the same thing
5. **Test** — Run existing tests after each change; fix any regressions immediately

Keep the scope focused. Don't expand beyond what was asked. Refactoring is not an excuse to rewrite everything.""",

    "plan": """\
You are in planning mode. Do NOT write or modify any code. Your job is to analyze, reason, and produce a plan.

Follow this approach:

1. **Understand the Goal** — Read the request carefully. Clarify ambiguities by stating your assumptions.
2. **Explore the Codebase** — Read the relevant files, understand the current architecture, data flow, and dependencies. List what you found.
3. **Identify Constraints** — Note technical constraints, existing patterns, potential risks, and edge cases.
4. **Propose Options** — Present 2-3 approaches with trade-offs (complexity, risk, effort, maintainability).
5. **Recommend** — Pick the best option and explain why.
6. **Detailed Plan** — Break the recommended approach into concrete, ordered implementation steps. For each step, name the files to change and describe what to do.

Output format: Use clear markdown headings for each section. Be specific — reference actual file paths, function names, and line numbers. The plan should be detailed enough that a developer (or a follow-up task) can execute it without further questions.

Remember: analysis and planning only. Do NOT create, edit, or delete any files.""",
}


def get_mode_prompt(mode: str, db=None) -> str:
    """Get the prompt for a mode. Checks DB for custom override first."""
    if db:
        custom = db.get_config(f"mode_prompt:{mode}")
        if custom and custom.strip():
            return custom.strip()
    return MODE_PROMPTS.get(mode, MODE_PROMPTS["feature"])


def get_default_mode_prompt(mode: str) -> str:
    """Get the built-in default prompt for a mode (ignoring DB overrides)."""
    return MODE_PROMPTS.get(mode, MODE_PROMPTS["feature"])


MODE_LABELS = {
    "bug": "Bug Fix",
    "feature": "Feature",
    "redesign": "Redesign",
    "plan": "Plan",
}

MODE_COLORS = {
    "bug": "#ef4444",
    "feature": "#e11d48",
    "redesign": "#f59e0b",
    "plan": "#3b82f6",
}
