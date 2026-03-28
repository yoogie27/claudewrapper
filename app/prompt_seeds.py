"""Built-in prompt library seeds — high-quality prompts for common development tasks."""

from __future__ import annotations

import hashlib


def _id(slug: str) -> str:
    """Deterministic ID from slug so seeds are stable across restarts."""
    return hashlib.md5(f"builtin:{slug}".encode()).hexdigest()


BUILTIN_PROMPTS: list[dict] = [
    # ── Code Quality ──
    {
        "id": _id("optimize-code"),
        "slash_command": "optimize",
        "title": "Optimize Code",
        "description": "Find performance bottlenecks and optimize for speed/memory",
        "category": "code-quality",
        "prompt": (
            "Analyze this codebase for performance optimization opportunities. Focus on:\n\n"
            "1. **Hot paths & bottlenecks** — identify code that runs frequently or processes large data sets. Profile-guided thinking: what would show up in a flame graph?\n"
            "2. **Algorithmic complexity** — flag any O(n^2) or worse patterns that could be O(n) or O(n log n). Look for nested loops over collections, repeated linear searches, and unnecessary sorting.\n"
            "3. **Memory efficiency** — find unnecessary copies, large allocations in loops, objects kept alive longer than needed, and opportunities for streaming/generators instead of collecting.\n"
            "4. **I/O and network** — identify sequential calls that could be batched or parallelized, missing caching for repeated expensive operations, and N+1 query patterns.\n"
            "5. **Data structure choices** — suggest better-fit data structures (e.g., set for membership checks, dict for lookups, deque for queues).\n"
            "6. **Language-specific idioms** — use built-in functions, comprehensions, or standard library utilities that are faster than hand-rolled equivalents.\n\n"
            "For each finding, explain the current cost, the proposed improvement, and the expected impact. Implement the optimizations directly — don't just suggest them."
        ),
    },
    {
        "id": _id("refactor"),
        "slash_command": "refactor",
        "title": "Refactor & Clean Up",
        "description": "Improve code structure, readability, and maintainability",
        "category": "code-quality",
        "prompt": (
            "Review and refactor this codebase for better structure and maintainability. Focus on:\n\n"
            "1. **Code smells** — long methods (>30 lines), deep nesting (>3 levels), god classes/modules, feature envy, and shotgun surgery patterns.\n"
            "2. **DRY violations** — duplicated logic that should be extracted. But don't over-abstract: three similar lines are better than a premature abstraction. Only extract when there are 3+ genuine duplicates.\n"
            "3. **Naming** — rename variables, functions, and classes to clearly express intent. A reader should understand what code does without reading the implementation.\n"
            "4. **Separation of concerns** — split modules that do too many things. Each function should do one thing well. Each module should have a clear, single responsibility.\n"
            "5. **Simplification** — replace complex conditionals with guard clauses or polymorphism. Flatten deeply nested code. Remove dead code and unused imports.\n"
            "6. **API design** — ensure public interfaces are intuitive, minimal, and hard to misuse. Prefer explicit parameters over implicit state.\n\n"
            "Make the changes directly. Preserve all existing behavior — this is a refactor, not a feature change. Run tests after if available."
        ),
    },
    {
        "id": _id("consolidate"),
        "slash_command": "consolidate",
        "title": "Consolidate & Deduplicate",
        "description": "Find and merge duplicated code, configs, and patterns",
        "category": "code-quality",
        "prompt": (
            "Scan the codebase for duplication and consolidation opportunities:\n\n"
            "1. **Duplicated logic** — find copy-pasted code blocks, similar functions with minor variations, and repeated patterns across files. Extract shared logic into well-named utilities.\n"
            "2. **Inconsistent patterns** — identify places where the same thing is done in different ways (e.g., error handling, validation, data transformation). Standardize on the best pattern.\n"
            "3. **Fragmented configuration** — find config values scattered across multiple files that should be centralized.\n"
            "4. **Redundant dependencies** — identify libraries that overlap in functionality or that could be replaced by built-in language features.\n"
            "5. **Parallel hierarchies** — find class/module structures that mirror each other and could be unified.\n\n"
            "For each consolidation, ensure the change is safe: verify all call sites are updated and behavior is preserved. Implement the changes directly."
        ),
    },
    {
        "id": _id("bad-patterns"),
        "slash_command": "bad-patterns",
        "title": "Find Bad Patterns",
        "description": "Detect anti-patterns, code smells, and problematic practices",
        "category": "code-quality",
        "prompt": (
            "Audit this codebase for anti-patterns and bad practices. Look for:\n\n"
            "1. **Error handling** — bare except/catch blocks, swallowed errors, error handling that hides bugs, inconsistent error propagation.\n"
            "2. **State management** — global mutable state, hidden side effects, action at a distance, shared state without synchronization.\n"
            "3. **Resource management** — unclosed files/connections/handles, missing cleanup in error paths, resource leaks in long-running processes.\n"
            "4. **Type confusion** — stringly-typed code, boolean parameters that should be enums, magic numbers, mixed return types.\n"
            "5. **Concurrency issues** — race conditions, missing locks, deadlock potential, shared mutable state across threads/async tasks.\n"
            "6. **Coupling** — circular dependencies, tight coupling between unrelated modules, concrete dependencies where abstractions would help.\n"
            "7. **Testing gaps** — untestable code (hard-coded dependencies, side effects in constructors), brittle tests that test implementation rather than behavior.\n\n"
            "Rank findings by severity (critical / warning / suggestion). Fix critical issues directly; document warnings with inline comments for the ones that need broader discussion."
        ),
    },
    # ── Architecture ──
    {
        "id": _id("architecture"),
        "slash_command": "architecture",
        "title": "Architecture Review",
        "description": "Analyze and improve software architecture and design",
        "category": "architecture",
        "prompt": (
            "Perform a comprehensive architecture review of this codebase:\n\n"
            "1. **Module structure** — map out the dependency graph. Identify circular dependencies, overly coupled modules, and modules with too many responsibilities. Suggest a cleaner module boundary layout.\n"
            "2. **Data flow** — trace how data moves through the system. Identify unnecessary transformations, places where data shape changes too often, and where a clearer data pipeline would help.\n"
            "3. **API boundaries** — review public interfaces between modules. Are they minimal, clear, and stable? Could internal details leak to consumers?\n"
            "4. **Scalability concerns** — identify bottlenecks that would appear under 10x load. Look for single points of failure, in-memory state that should be externalized, and synchronous operations that block.\n"
            "5. **Extensibility** — where would a new feature require changes in many places? Suggest patterns that would make common extensions easier (plugin points, strategy pattern, event-driven).\n"
            "6. **Layering** — verify that business logic is separated from infrastructure concerns (DB, HTTP, file I/O). Flag any business rules embedded in controllers/routes or infrastructure code.\n\n"
            "Provide a clear summary of the architecture's strengths and weaknesses, then implement the highest-impact improvements."
        ),
    },
    {
        "id": _id("redesign"),
        "slash_command": "redesign",
        "title": "Redesign Component",
        "description": "Rethink and restructure a specific component or subsystem",
        "category": "architecture",
        "prompt": (
            "Redesign the targeted component/subsystem from first principles:\n\n"
            "1. **Current state analysis** — document what the component does, its inputs/outputs, its dependencies, and its pain points.\n"
            "2. **Requirements extraction** — from the current behavior and usage patterns, derive the actual requirements (not just what was built, but what's needed).\n"
            "3. **Design alternatives** — propose 2-3 design approaches. For each, describe the trade-offs in complexity, performance, testability, and extensibility.\n"
            "4. **Selected approach** — pick the best design and explain why. Implement it, migrating all existing callers.\n"
            "5. **Migration safety** — ensure backward compatibility during the transition if needed. Mark deprecated code paths clearly.\n\n"
            "Focus on simplicity: the best redesign often removes code rather than adding it."
        ),
    },
    # ── Security ──
    {
        "id": _id("security"),
        "slash_command": "security",
        "title": "Security Review",
        "description": "Audit for vulnerabilities: injection, auth, data exposure",
        "category": "security",
        "prompt": (
            "Perform a thorough security audit of this codebase, checking for:\n\n"
            "1. **Injection vulnerabilities** — SQL injection (raw queries with string formatting), command injection (shell calls with user input), XSS (unescaped output in HTML/JS), template injection, path traversal.\n"
            "2. **Authentication & Authorization** — broken access controls, missing auth checks on sensitive endpoints, privilege escalation paths, insecure session management, hardcoded credentials.\n"
            "3. **Data exposure** — sensitive data in logs, error messages that leak internals, API responses that return more data than needed, secrets in source code or config files.\n"
            "4. **Cryptographic issues** — weak hashing algorithms, hardcoded keys/salts, insecure random number generation, missing encryption for sensitive data at rest or in transit.\n"
            "5. **Input validation** — missing or insufficient validation at system boundaries, type coercion vulnerabilities, oversized input handling, file upload risks.\n"
            "6. **Dependency risks** — known CVEs in dependencies, outdated packages with security patches available, unnecessary dependencies that increase attack surface.\n"
            "7. **Configuration** — debug mode in production, overly permissive CORS, missing security headers, default credentials.\n\n"
            "Classify each finding as Critical/High/Medium/Low. Fix Critical and High issues directly. Document Medium and Low with specific remediation steps."
        ),
    },
    {
        "id": _id("pentest"),
        "slash_command": "pentest",
        "title": "Penetration Test Analysis",
        "description": "Simulate attacker perspective to find exploitable vulnerabilities",
        "category": "security",
        "prompt": (
            "Analyze this codebase from an attacker's perspective. Think like a penetration tester:\n\n"
            "1. **Attack surface mapping** — identify all entry points: HTTP endpoints, CLI args, file inputs, environment variables, IPC channels. Which accept untrusted input?\n"
            "2. **Input-to-sink tracing** — for each entry point, trace user-controlled data through the code to dangerous sinks (SQL queries, shell commands, file operations, HTML output, deserialization).\n"
            "3. **Authentication bypass** — look for ways to skip auth: direct object references, parameter manipulation, race conditions in auth flows, JWT/token weaknesses.\n"
            "4. **Privilege escalation** — can a low-privilege user access admin functionality? Are there IDOR vulnerabilities? Can users modify their own roles?\n"
            "5. **Business logic flaws** — identify abuse scenarios: can workflows be completed out of order? Can rate limits be bypassed? Can financial calculations be manipulated?\n"
            "6. **Information disclosure** — error messages, stack traces, timing differences, HTTP headers that reveal technology stack or internal structure.\n\n"
            "For each vulnerability found, provide: description, proof-of-concept exploitation steps, impact assessment, and remediation. Fix the critical ones directly."
        ),
    },
    {
        "id": _id("harden"),
        "slash_command": "harden",
        "title": "Security Hardening",
        "description": "Strengthen defenses: input validation, auth, error handling",
        "category": "security",
        "prompt": (
            "Harden this codebase against common attack vectors:\n\n"
            "1. **Input validation** — add strict validation at every system boundary. Validate types, ranges, lengths, and formats. Use allowlists over denylists.\n"
            "2. **Output encoding** — ensure all dynamic output is properly encoded for its context (HTML, JS, SQL, shell, URL).\n"
            "3. **Error handling** — replace verbose error messages with safe, generic responses. Log the details server-side. Never expose stack traces, file paths, or internal state.\n"
            "4. **Authentication** — ensure consistent auth checks. Add rate limiting to auth endpoints. Ensure secure password handling.\n"
            "5. **Authorization** — verify permission checks on every state-changing operation. Ensure users can only access their own resources.\n"
            "6. **Headers & config** — add security headers (CSP, HSTS, X-Frame-Options). Disable unnecessary HTTP methods. Set secure cookie flags.\n\n"
            "Implement all changes directly. Each hardening measure should be minimal and focused — don't add unnecessary complexity."
        ),
    },
    {
        "id": _id("owasp"),
        "slash_command": "owasp",
        "title": "OWASP Top 10 Audit",
        "description": "Check against OWASP Top 10 web application security risks",
        "category": "security",
        "prompt": (
            "Audit this codebase against the OWASP Top 10 (2021) web application security risks:\n\n"
            "1. **A01: Broken Access Control** — missing authorization checks, IDOR, CORS misconfiguration, forced browsing to unauthenticated pages, privilege escalation.\n"
            "2. **A02: Cryptographic Failures** — sensitive data transmitted in cleartext, weak algorithms (MD5/SHA1 for passwords), missing encryption at rest, hardcoded secrets.\n"
            "3. **A03: Injection** — SQL injection, command injection, LDAP injection, XSS, template injection. Trace all user input to dangerous sinks.\n"
            "4. **A04: Insecure Design** — missing rate limiting, no defense in depth, trust boundary violations, missing threat modeling for critical flows.\n"
            "5. **A05: Security Misconfiguration** — default credentials, unnecessary features enabled, overly permissive cloud/firewall rules, missing security headers, verbose error messages.\n"
            "6. **A06: Vulnerable & Outdated Components** — known CVEs in dependencies, unmaintained libraries, components with unnecessary privileges.\n"
            "7. **A07: Identification & Authentication Failures** — weak passwords allowed, missing brute-force protection, session fixation, credential stuffing exposure.\n"
            "8. **A08: Software & Data Integrity Failures** — unsigned updates, insecure deserialization, CI/CD pipeline security, untrusted plugins/extensions.\n"
            "9. **A09: Security Logging & Monitoring Failures** — missing audit logs, unlogged auth failures, no alerting for suspicious activity, logs not protected from tampering.\n"
            "10. **A10: Server-Side Request Forgery (SSRF)** — URL fetching without validation, internal network access via user-controlled URLs, DNS rebinding.\n\n"
            "For each category, state whether the codebase is affected, provide evidence, and rate severity. Fix Critical and High issues directly."
        ),
    },
    {
        "id": _id("secrets-scan"),
        "slash_command": "secrets",
        "title": "Secrets & Credentials Scan",
        "description": "Find hardcoded secrets, API keys, tokens, and passwords",
        "category": "security",
        "prompt": (
            "Scan this codebase for exposed secrets and credentials:\n\n"
            "1. **Hardcoded credentials** — passwords, API keys, tokens, connection strings embedded in source code, config files, or comments.\n"
            "2. **Environment leaks** — .env files committed to git, environment variables logged or exposed in error messages, secrets in CI/CD configs.\n"
            "3. **Git history** — check for secrets that were previously committed and later removed (they're still in history). Use `git log -p --all -S 'password\\|secret\\|api_key\\|token'`.\n"
            "4. **Config files** — check all YAML, JSON, TOML, INI, and properties files for credentials, including test/example configs that might contain real values.\n"
            "5. **Private keys** — look for PEM, P12, JKS files. Check for RSA/SSH private keys embedded in code or config.\n"
            "6. **Third-party service credentials** — database passwords, AWS keys, GCP service account JSON, Stripe keys, SMTP credentials, OAuth secrets.\n\n"
            "For each finding, assess the blast radius (what does this credential grant access to?). Replace hardcoded secrets with environment variable references. Add patterns to .gitignore to prevent future commits of secret files."
        ),
    },
    {
        "id": _id("threat-model"),
        "slash_command": "threat-model",
        "title": "Threat Modeling",
        "description": "Identify threats, attack vectors, and trust boundaries",
        "category": "security",
        "prompt": (
            "Perform threat modeling on this codebase using STRIDE methodology:\n\n"
            "1. **System decomposition** — identify components, data stores, data flows, and trust boundaries. Map where untrusted input enters the system and where sensitive data leaves.\n"
            "2. **Spoofing** — can an attacker impersonate a legitimate user, service, or component? Check authentication at every trust boundary.\n"
            "3. **Tampering** — can data be modified in transit or at rest without detection? Check for unsigned data, missing integrity checks, and unprotected configuration.\n"
            "4. **Repudiation** — can users deny their actions? Check audit logging for completeness. Are all state-changing operations logged with actor, action, and timestamp?\n"
            "5. **Information Disclosure** — can sensitive data leak through error messages, logs, side channels (timing), or overly broad API responses?\n"
            "6. **Denial of Service** — can an attacker exhaust resources? Check for unbounded queries, missing rate limits, file upload size limits, and regex DoS (ReDoS).\n"
            "7. **Elevation of Privilege** — can a low-privilege user gain higher access? Check role enforcement, default permissions, and admin functionality exposure.\n\n"
            "Produce a threat matrix listing each threat, its likelihood, impact, and current mitigation status. Implement fixes for unmitigated high-risk threats."
        ),
    },
    {
        "id": _id("api-security"),
        "slash_command": "api-security",
        "title": "API Security Audit",
        "description": "Audit REST/GraphQL APIs for security weaknesses",
        "category": "security",
        "prompt": (
            "Audit all API endpoints in this codebase for security issues:\n\n"
            "1. **Authentication** — which endpoints require auth? Which are public? Verify the split is intentional. Check for endpoints that should require auth but don't.\n"
            "2. **Authorization** — after auth, are permissions checked? Can user A access user B's data by changing an ID in the URL? Test for IDOR on every endpoint that takes a resource ID.\n"
            "3. **Input validation** — for each endpoint, what inputs does it accept? Are they validated for type, length, format, and range? Check path params, query params, headers, and body.\n"
            "4. **Rate limiting** — are there limits on request frequency? Which endpoints are most abuse-prone (login, signup, password reset, data export)?\n"
            "5. **Data exposure** — do API responses include more data than the client needs? Are internal fields (IDs, timestamps, metadata) leaking? Check for mass assignment vulnerabilities.\n"
            "6. **Error handling** — do error responses reveal internal details (stack traces, SQL errors, file paths)? Are error responses consistent in format?\n"
            "7. **HTTP methods** — are only intended methods allowed? Can you PUT/DELETE on read-only resources? Are OPTIONS responses safe?\n\n"
            "List all endpoints with their security posture. Fix any vulnerabilities found."
        ),
    },
    {
        "id": _id("supply-chain"),
        "slash_command": "supply-chain",
        "title": "Supply Chain Security",
        "description": "Audit dependencies, build pipeline, and third-party risks",
        "category": "security",
        "prompt": (
            "Audit the software supply chain of this project:\n\n"
            "1. **Dependency vulnerabilities** — check all direct and transitive dependencies for known CVEs. Use the package manager's audit tool (npm audit, pip-audit, cargo audit, etc.).\n"
            "2. **Dependency hygiene** — identify unmaintained packages (no updates in 2+ years), packages with very few maintainers, and packages that pull in excessive transitive dependencies.\n"
            "3. **Lock files** — verify lock files exist and are committed. Check for drift between lock file and manifest. Ensure reproducible builds.\n"
            "4. **Build integrity** — check CI/CD configuration for security: pinned action versions, secret handling, artifact signing, branch protection.\n"
            "5. **Typosquatting risk** — verify package names are correct and from official sources. Check for similarly-named malicious packages.\n"
            "6. **Permissions** — do dependencies request more permissions than they need? Check for packages that execute postinstall scripts, access the network, or read environment variables.\n\n"
            "Update vulnerable dependencies where possible. Document any that can't be updated with justification and compensating controls."
        ),
    },
    # ── Bug Hunting ──
    {
        "id": _id("bugs"),
        "slash_command": "bugs",
        "title": "Bug Hunt",
        "description": "Systematically find bugs, edge cases, and failure modes",
        "category": "bugs",
        "prompt": (
            "Hunt for bugs in this codebase. Systematically check for:\n\n"
            "1. **Edge cases** — empty inputs, null/undefined/None values, zero-length collections, boundary values (0, -1, MAX_INT), unicode/special characters, concurrent access.\n"
            "2. **Off-by-one errors** — loop boundaries, string slicing, array indexing, pagination, range calculations.\n"
            "3. **Race conditions** — shared state accessed from multiple threads/coroutines without synchronization, TOCTOU (time-of-check-to-time-of-use) bugs, double-submit issues.\n"
            "4. **Error path bugs** — exceptions that leave state inconsistent, cleanup code that doesn't run on error, cascading failures from unhandled errors.\n"
            "5. **Type coercion** — implicit conversions that lose data or change semantics, integer overflow/underflow, floating point comparison issues.\n"
            "6. **State management** — stale caches, state that gets out of sync between components, initialization order dependencies, forgotten state resets.\n"
            "7. **Integration issues** — API contract mismatches, incompatible data formats between components, missing error handling at service boundaries.\n\n"
            "For each bug found, explain the trigger condition, the incorrect behavior, and the fix. Implement fixes directly."
        ),
    },
    {
        "id": _id("debug"),
        "slash_command": "debug",
        "title": "Debug Issue",
        "description": "Diagnose and fix a specific bug or unexpected behavior",
        "category": "bugs",
        "prompt": (
            "Help me debug an issue in this codebase. Follow a systematic approach:\n\n"
            "1. **Reproduce** — understand the expected vs actual behavior. What are the exact steps to trigger it?\n"
            "2. **Isolate** — narrow down the problem area. Use binary search: which component/layer is the bug in? Add logging or assertions to pinpoint the exact location.\n"
            "3. **Root cause** — don't stop at the symptom. Ask \"why?\" until you reach the actual root cause. The fix should address the cause, not patch the symptom.\n"
            "4. **Fix** — implement the minimal, targeted fix. Don't refactor surrounding code or fix unrelated issues in the same change.\n"
            "5. **Verify** — confirm the fix resolves the issue and doesn't introduce regressions. Check related code paths for the same class of bug.\n\n"
            "Describe the bug you're seeing and I'll work through this process to find and fix it."
        ),
    },
    # ── Testing ──
    {
        "id": _id("test-coverage"),
        "slash_command": "tests",
        "title": "Add Test Coverage",
        "description": "Write tests for uncovered code paths and edge cases",
        "category": "testing",
        "prompt": (
            "Analyze the codebase and add comprehensive test coverage:\n\n"
            "1. **Identify untested code** — find functions, branches, and error paths that lack tests. Prioritize business-critical logic and complex algorithms.\n"
            "2. **Happy path tests** — ensure every public function has at least one test covering the normal/expected use case.\n"
            "3. **Edge cases** — add tests for boundary values, empty inputs, error conditions, and unusual but valid inputs.\n"
            "4. **Error handling** — test that errors are properly caught, reported, and recovered from. Test that invalid inputs produce clear error messages.\n"
            "5. **Integration points** — test interactions between components, especially at API/service boundaries.\n"
            "6. **Regression tests** — if there are known past bugs, add tests that would catch them if they recurred.\n\n"
            "Follow the project's existing test patterns and framework. Write tests that are readable, fast, and test behavior (not implementation). Each test should have a clear name describing what it verifies."
        ),
    },
    # ── Documentation ──
    {
        "id": _id("document"),
        "slash_command": "document",
        "title": "Document Code",
        "description": "Add documentation, docstrings, and usage examples",
        "category": "documentation",
        "prompt": (
            "Add documentation to improve code understandability:\n\n"
            "1. **Module-level docs** — add a brief description at the top of each module explaining its purpose and how it fits in the system.\n"
            "2. **Public API docs** — add docstrings to all public functions/classes describing: what it does, parameters, return values, exceptions raised, and a usage example where helpful.\n"
            "3. **Complex logic** — add inline comments only where the logic is non-obvious. Explain *why*, not *what*. If code needs a comment explaining what it does, consider rewriting it to be self-documenting.\n"
            "4. **Architecture overview** — if a README exists, update it with current architecture description, setup instructions, and key design decisions.\n"
            "5. **Type annotations** — add type hints to function signatures that lack them, especially public APIs.\n\n"
            "Keep documentation concise. One clear sentence beats three vague ones. Match the existing documentation style in the project."
        ),
    },
    # ── Performance ──
    {
        "id": _id("profile"),
        "slash_command": "profile",
        "title": "Performance Profile",
        "description": "Identify slow code paths and optimize critical sections",
        "category": "performance",
        "prompt": (
            "Profile this codebase for performance issues:\n\n"
            "1. **Critical paths** — identify the most performance-sensitive code paths (request handling, data processing pipelines, startup time).\n"
            "2. **Database queries** — find N+1 queries, missing indexes, unnecessary JOINs, queries that fetch more data than needed, and missing pagination.\n"
            "3. **I/O bottlenecks** — identify synchronous I/O in async contexts, sequential operations that could be parallel, missing connection pooling.\n"
            "4. **Memory usage** — find memory leaks, large allocations in hot paths, data structures that grow unbounded, unnecessary data copying.\n"
            "5. **Caching opportunities** — identify expensive operations with stable results that should be cached. Suggest cache invalidation strategies.\n"
            "6. **Startup time** — find slow initialization, eager loading that could be lazy, heavy imports that could be deferred.\n\n"
            "Implement the optimizations with the biggest impact. Include before/after complexity analysis for each change."
        ),
    },
    # ── Code Review ──
    {
        "id": _id("review"),
        "slash_command": "review",
        "title": "Code Review",
        "description": "Thorough code review covering quality, correctness, and style",
        "category": "review",
        "prompt": (
            "Do a thorough code review as if you're reviewing a PR from a team member:\n\n"
            "1. **Correctness** — does the code do what it claims? Are there logic errors, missed edge cases, or incorrect assumptions?\n"
            "2. **Design** — is this the right approach? Is it over-engineered or under-engineered? Does it fit the codebase's existing patterns?\n"
            "3. **Readability** — can a new team member understand this code? Are names clear? Is the flow easy to follow?\n"
            "4. **Robustness** — how does it handle errors, invalid input, and unexpected state? Will it fail gracefully?\n"
            "5. **Performance** — any obvious performance issues? Unnecessary work in hot paths?\n"
            "6. **Security** — any input validation gaps, injection risks, or data exposure concerns?\n"
            "7. **Testing** — is it testable? Are tests included? Do they cover meaningful scenarios?\n\n"
            "Give feedback at three levels: must-fix (blocking issues), should-fix (improvements), and nit (minor style preferences). Implement must-fix and should-fix changes directly."
        ),
    },
    {
        "id": _id("review-diff"),
        "slash_command": "review-diff",
        "title": "Review Recent Changes",
        "description": "Review the latest git diff for issues and improvements",
        "category": "review",
        "prompt": (
            "Review the recent changes in this repository. Run `git diff` and `git log` to see what changed, then:\n\n"
            "1. **Correctness** — verify the changes work as intended. Check for off-by-one errors, missed edge cases, and logic flaws.\n"
            "2. **Completeness** — are all necessary changes included? Any files that should have been updated but weren't?\n"
            "3. **Regressions** — could these changes break existing functionality? Check callers of modified functions.\n"
            "4. **Style consistency** — do the changes match the codebase's existing patterns and conventions?\n"
            "5. **Test coverage** — are the changes tested? If tests exist, do they need updating?\n\n"
            "Provide specific, actionable feedback. Fix any issues you find."
        ),
    },
    # ── DevOps & Dependencies ──
    {
        "id": _id("deps"),
        "slash_command": "deps",
        "title": "Dependency Audit",
        "description": "Review and clean up project dependencies",
        "category": "devops",
        "prompt": (
            "Audit this project's dependencies:\n\n"
            "1. **Unused dependencies** — find packages that are imported in the config but never used in code. Remove them.\n"
            "2. **Duplicate functionality** — find multiple packages that serve the same purpose. Consolidate to one.\n"
            "3. **Heavy dependencies** — identify large packages imported for small features. Consider replacing with lighter alternatives or built-in functionality.\n"
            "4. **Version constraints** — check for overly restrictive or overly loose version pins. Recommend appropriate version ranges.\n"
            "5. **Security** — check for known vulnerable versions that should be updated.\n"
            "6. **Missing dependencies** — find imports that aren't listed in the dependency manifest.\n\n"
            "Make the changes to the dependency files directly. Ensure the project still builds and runs after changes."
        ),
    },
    {
        "id": _id("cleanup"),
        "slash_command": "cleanup",
        "title": "Project Cleanup",
        "description": "Remove dead code, unused files, and accumulated cruft",
        "category": "code-quality",
        "prompt": (
            "Clean up this project by removing accumulated cruft:\n\n"
            "1. **Dead code** — find functions, classes, and methods that are never called. Remove them entirely (no commenting out).\n"
            "2. **Unused imports** — remove all unused imports across the codebase.\n"
            "3. **Unused files** — find source files that are not imported or referenced anywhere. Verify they're truly unused before removing.\n"
            "4. **TODO/FIXME/HACK comments** — catalog all TODO-style comments. Resolve the ones that are quick fixes; report the rest.\n"
            "5. **Stale configuration** — find config values that are no longer referenced, feature flags for long-shipped features, and commented-out code blocks.\n"
            "6. **Temporary/debug code** — remove console.log/print statements, debug flags, test data, and other development artifacts.\n\n"
            "Be careful with dead code removal: verify via search that code is truly unreachable before deleting. Check for dynamic dispatch, reflection, and string-based references."
        ),
    },
    {
        "id": _id("explain"),
        "slash_command": "explain",
        "title": "Explain Codebase",
        "description": "Walk through how the code works, end to end",
        "category": "documentation",
        "prompt": (
            "Explain how this codebase works. Provide a clear, structured walkthrough:\n\n"
            "1. **High-level overview** — what does this project do? What problem does it solve?\n"
            "2. **Architecture** — how is it structured? What are the main components and how do they interact?\n"
            "3. **Data flow** — trace a typical request/operation from start to finish. What happens at each step?\n"
            "4. **Key design decisions** — what patterns and approaches were chosen? Why might they have been chosen?\n"
            "5. **Entry points** — where does execution start? How is the application configured and bootstrapped?\n"
            "6. **Extension points** — where would you add new features? What's the pattern for adding a new endpoint/command/module?\n\n"
            "Use concrete file and function references. Keep explanations concise but thorough enough for a new developer to get oriented quickly."
        ),
    },
    {
        "id": _id("types"),
        "slash_command": "types",
        "title": "Fix Types & Annotations",
        "description": "Add or fix type annotations, resolve type errors",
        "category": "code-quality",
        "prompt": (
            "Improve type safety across the codebase:\n\n"
            "1. **Missing annotations** — add type annotations to function signatures, especially public APIs and module boundaries.\n"
            "2. **Type errors** — find and fix type mismatches, incorrect casts, and unsafe operations.\n"
            "3. **Narrow types** — replace broad types (Any, object, dict) with specific types where possible. Use TypedDict, NamedTuple, or dataclasses for structured data.\n"
            "4. **Null safety** — find places where None/null/undefined can sneak through without checks. Add proper guards.\n"
            "5. **Generic types** — use generics where functions work with multiple types instead of using Any.\n\n"
            "Run the type checker if one is configured. Fix all errors it reports."
        ),
    },
]
