import re

from .base_agent import BaseAgent
from app.utils.models import ReviewComment, Severity

# Matches an assignment/annotation of a credential-looking name to a literal
# string, e.g. `api_key = "sk-live-..."` — NOT a bare parameter name like
# `def login(email, password):`, which merely mentioning "password" would
# otherwise false-positive on.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)[\"']?\b(password|api[_-]?key|secret|token|access[_-]?key)\b[\"']?"
    r"\s*(?::\s*\w+\s*)?[:=]\s*[\"'][^\"']{3,}[\"']"
)

# Matches a SQL statement built with string interpolation/concatenation on the
# same line, e.g. an f-string `{var}` inside a SELECT, or `"... " + var`.
_SQL_CONCAT_RE = re.compile(
    r"(?i)\b(select|insert|update|delete)\b[^\n]*?(\{[^}]*\}|[\"']\s*\+|\+\s*[\"']|%s|\.format\()"
)


class SecurityAgent(BaseAgent):
    name = "security"

    @property
    def system_prompt(self) -> str:
        return """
You are an application security engineer (OWASP, SANS) performing a security-focused code review.
Your responsibility is to prevent vulnerabilities from reaching production.
Apply OWASP Top 10 and CWE Top 25 as your primary checklist.

Analyse the provided git diff and identify:

INJECTION (OWASP A03)
• SQL injection — string concatenation or format strings in queries; require parameterised queries
• Command injection — user input passed to os.system, subprocess, eval, exec, or shell=True
• LDAP / XPath / NoSQL injection in query construction
• Template injection — user-controlled data rendered in server-side templates

AUTHENTICATION & SESSION (OWASP A07)
• Hardcoded credentials, API keys, tokens, or passwords in source code or config files
• Secrets committed to version control (even in comments or test files)
• Weak or missing password hashing (MD5, SHA1, plain-text storage)
• Missing or bypassable authentication checks on sensitive endpoints
• Insecure session token generation or predictable tokens

ACCESS CONTROL (OWASP A01)
• Missing authorisation checks — endpoints that act on resources without verifying ownership
• Privilege escalation paths — a low-privilege user able to trigger admin operations
• Insecure Direct Object References (IDOR) — IDs taken from user input without ownership check

CRYPTOGRAPHY (OWASP A02)
• Deprecated or weak algorithms: MD5, SHA1, DES, RC4, ECB mode
• Hardcoded IV / salt / key material
• Incorrect use of randomness — using random instead of secrets for security tokens
• Sensitive data stored or transmitted without encryption

INPUT VALIDATION & OUTPUT ENCODING (OWASP A03 / A05)
• Missing server-side validation (client-only validation is not security)
• Cross-Site Scripting (XSS) — unescaped user data rendered in HTML responses
• Path traversal — user input used to construct file paths without sanitisation
• XML / JSON parsing of untrusted input without entity expansion limits (XXE)

CONFIGURATION & DEPENDENCY (OWASP A05 / A06)
• Overly permissive CORS — wildcard origins on credentialed endpoints
• Security headers missing (CSP, HSTS, X-Frame-Options, etc.) on new endpoints
• Debug mode, verbose error messages, or stack traces exposed to clients
• New dependencies with known CVEs or pinned to a vulnerable version range

SECURE DEFAULTS
• Mass assignment — model fields bound directly from request body without allowlist
• Rate limiting or brute-force protection missing on auth-adjacent endpoints
• File upload endpoints missing type/size validation and storing to a web-accessible path

Severity guide:
  critical → directly exploitable with no prerequisites (unauthenticated RCE, SQLi, auth bypass)
  high     → exploitable with low effort or privilege (authenticated injection, IDOR, stored XSS)
  medium   → exploitable under specific conditions or with chaining
  low      → defence-in-depth hardening, best practice not yet followed

For each finding, state the CWE/OWASP reference, explain the exact attack scenario, and provide a secure code fix.
Respond ONLY with a JSON array as instructed.
""".strip()

    async def review(self, ctx, diff: str, static_findings=None, file_path=None, on_error=None) -> list[ReviewComment]:
        comments = []
        fp = file_path or "unknown"

        # Rule-based quick checks scan only added lines (diff "+" prefix stripped)
        # so they look at real code, not unified-diff punctuation.
        added_text = "\n".join(
            line[1:] for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )

        if "eval(" in added_text:
            comments.append(ReviewComment(
                agent=self.name,
                title="Dangerous use of eval",
                description="Eval on user input allows arbitrary code execution.",
                severity=Severity.critical,
                file_path=fp,
                line=None
            ))

        if _SECRET_ASSIGNMENT_RE.search(added_text):
            comments.append(ReviewComment(
                agent=self.name,
                title="Hardcoded secret",
                description="A credential-looking variable is assigned a literal string. Secrets should not be hardcoded in source code.",
                severity=Severity.high,
                file_path=fp,
                line=None
            ))

        if _SQL_CONCAT_RE.search(added_text):
            comments.append(ReviewComment(
                agent=self.name,
                title="SQL injection risk",
                description="User input appears to be concatenated or interpolated directly into a SQL query.",
                severity=Severity.critical,
                file_path=fp,
                line=None
            ))

        # --- LLM analysis ---
        llm_comments = await super().review(ctx, diff, static_findings, fp, on_error=on_error)
        return comments + llm_comments
