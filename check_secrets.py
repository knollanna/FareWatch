#!/usr/bin/env python3
"""Pre-commit check: block a commit that stages a real secret.

Built after the app password (`APP_PASSWORD is currently \\`hunter2example\\``)
sat in docs/project-context.md from that file's first commit, in a repo
that later went public — the value itself, in prose, not a code path. A
narrow check for one leaked value wouldn't have caught the *next* one;
this is the general version.

Three independent signals, any one of which blocks:
  1. Known secret-format patterns (AWS, Slack, Stripe, GitHub, a bare JWT,
     a PEM private key header, this project's own Duffel live/test token
     shape).
  2. A credential-shaped variable assigned a literal string in code
     (PASSWORD/SECRET/API_KEY/TOKEN/... = "...") instead of read from the
     environment.
  3. A credential keyword with a nearby quoted value ("password `...`",
     "key: `...`") that also looks secret-shaped (not a field name, route
     pattern, or filename) and clears an entropy floor — this is the shape
     the real incident had (a markdown doc, not code), and the catch-all
     for a secret format not covered by #1. All three conditions matter:
     keyword-proximity alone flagged "token across `watches`" (an ordinary
     word), and entropy alone can't tell a real secret apart from a quoted
     filename without the shape check first.

Checks only ADDED lines in the staged diff — an already-committed false
positive shouldn't re-block every future commit to that file. `.env` /
`.env.example` staged at all is an automatic block regardless of content;
`.env` should never be staged and `.env.example` should never hold a real
value.

Deliberately noisy over silent: a false positive costs one look at a diff;
a missed real secret costs a public repo. Override for a confirmed false
positive:

    SKIP_SECRET_CHECK=1 git commit ...

    .githooks/pre-commit -> check_secrets.py (this file)
"""

import math
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent

# Files that must never be staged at all, regardless of content.
FORBIDDEN_PATHS = {".env"}
FORBIDDEN_PATH_PREFIXES = ()  # e.g. add a secrets/ dir here if one shows up

# (label, pattern) — known secret shapes. Ordered roughly by specificity.
KNOWN_PATTERNS = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Stripe live/test key", re.compile(r"sk_(live|test)_[A-Za-z0-9]{10,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{10,}")),
    ("PEM private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Duffel live/test token", re.compile(r"duffel_(live|test)_[A-Za-z0-9]{10,}")),
    ("generic API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
]

# The keyword can be a prefix OR a suffix of the identifier — this codebase's
# own convention is suffix-style (DUFFEL_API_TOKEN, APP_PASSWORD), which a
# leading \b before the keyword would miss entirely (underscore is a word
# character, so DUFFEL_API_TOKEN is one token with no boundary before TOKEN).
CREDENTIAL_VAR_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:PASSWORD|SECRET|KEY|TOKEN|PWD|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*[:=]\s*[\"']([^\"'\s]{6,})[\"']"
)
# Reading from the environment is the whole point — don't flag it.
ENV_READ_RE = re.compile(r"os\.environ|getenv|process\.env|ENV\[")

# No required linking word ("is"/":"/"=") — the real incident this is modeled
# on was "password `hunter2example`.", keyword directly against the quote.
# Bounded distance (not "anywhere on the line") so a line that just happens to
# mention a credential env-var NAME and, much later, an unrelated quoted
# filename doesn't false-positive — e.g. Bearer {SENDGRID_API_KEY}" ...
# "application/json" on one line, keyword and quote nowhere near each other.
# 8-char minimum on the captured value — the real incident's value
# ("hunter2example") is 13 characters, but a short one should still count.
CREDENTIAL_PROSE_RE = re.compile(
    r"\b(password|secret|api[_ ]?key|token|credential)s?\b.{0,30}?"
    r"[`\"']([A-Za-z0-9+/=_.<>-]{6,})[`\"']",
    re.IGNORECASE,
)

MIN_ENTROPY = 3.3  # bits/char; a real key/token lands well above this, prose doesn't

# Words that ARE the candidate itself, not a secret — an HTML
# `type="password"` attribute is the single most common shape this hits.
CREDENTIAL_WORDS = {"password", "secret", "token", "key", "credential", "credentials"}
SCREAMING_SNAKE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _looks_like_a_secret(candidate: str) -> bool:
    """False for anything that reads as a name/path/placeholder rather than a
    plausible credential value — the shape check that turns "keyword near a
    quoted string" from noise into a real signal.

    Excludes, in order:
      - the keyword itself as the value (`type="password"`)
      - an ALL_CAPS identifier (an env-var NAME, not a value — this codebase
        documents them in backticks constantly, and a bare identifier has no
        case variation to hide a real secret's entropy anyway)
      - anything all-lowercase AND shaped like an identifier/path/placeholder/
        filename (underscore, hyphen, slash, angle bracket, dot) —
        `client_name/email/token`, `firstname-lastname-xxxx`,
        `/client/<token>`, `.env.example` all fail this on shape alone. A
        real secret is usually mixed-case or long-and-opaque; one that
        happens to be lowercase-only with none of those separators still
        passes through to the entropy check.
    """
    if candidate.lower() in CREDENTIAL_WORDS:
        return False
    if SCREAMING_SNAKE_RE.match(candidate):
        return False
    if candidate == candidate.lower() and re.search(r"[_/<>.-]", candidate):
        return False
    return True


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def staged_paths() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--no-color"],
        cwd=REPO, capture_output=True, text=True, check=False,
    ).stdout
    return [p for p in out.splitlines() if p]


def staged_added_lines() -> dict[str, list[tuple[int, str]]]:
    """{path: [(line_no, text), ...]} for every ADDED line in the staged diff."""
    out = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--no-color"],
        cwd=REPO, capture_output=True, text=True, check=False,
    ).stdout

    result: dict[str, list[tuple[int, str]]] = {}
    current_file = None
    current_line = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[4:]
            current_file = None if path == "/dev/null" else path[2:]  # strip "b/"
            continue
        if line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            current_line = int(m.group(1)) if m else None
            continue
        if current_file and current_line is not None and line.startswith("+") and not line.startswith("+++"):
            result.setdefault(current_file, []).append((current_line, line[1:]))
            current_line += 1
        elif current_file and current_line is not None and not line.startswith("-"):
            current_line += 1
    return result


def check_line(text: str) -> list[str]:
    """Return every reason this line looks like it contains a secret."""
    reasons = []

    for label, pattern in KNOWN_PATTERNS:
        if pattern.search(text):
            reasons.append(label)

    if not ENV_READ_RE.search(text):
        m = CREDENTIAL_VAR_RE.search(text)
        if m:
            reasons.append(f"credential variable assigned a literal ({m.group(1)})")

    # Signals 3 and 4 are one check: a keyword with a nearby quoted string
    # (bounded distance, not "keyword anywhere on the line" — that's what let
    # "application/json", quoted far from an unrelated {SENDGRID_API_KEY}
    # interpolation on the same line, read as suspicious) that ALSO looks
    # secret-shaped (not a field name, route pattern, or the word "password"
    # itself) AND clears the entropy floor. Entropy is required here, not
    # optional — without it, an ordinary word near "token" ("token across
    # `watches` and `hotel_watches`") reads as a hit on shape alone; low
    # entropy is exactly what tells "watches" apart from "hunter2example".
    for keyword, candidate in CREDENTIAL_PROSE_RE.findall(text):
        if _looks_like_a_secret(candidate) and shannon_entropy(candidate) >= MIN_ENTROPY:
            reasons.append(f"credential-shaped, high-entropy value near {keyword.lower()!r} ({candidate[:12]}...)")
            break

    return reasons


def main() -> int:
    if os.environ.get("SKIP_SECRET_CHECK"):
        return 0

    forbidden_hits = [
        p for p in staged_paths()
        if p in FORBIDDEN_PATHS or any(p.startswith(pre) for pre in FORBIDDEN_PATH_PREFIXES)
    ]
    if forbidden_hits:
        print("\n\U0001F6D1 check_secrets: staged a file that must never be committed:\n")
        for p in forbidden_hits:
            print(f"  {p}")
        print(
            "\nThis path is gitignored for a reason. If it's staged anyway, someone used "
            "`git add -f`. Unstage it:\n\n"
            f"    git restore --staged {' '.join(forbidden_hits)}\n"
        )
        return 1

    added = staged_added_lines()
    hits = []
    for path, lines in added.items():
        for line_no, text in lines:
            for reason in check_line(text):
                hits.append((path, line_no, reason, text.strip()))

    if not hits:
        return 0

    print("\n\U0001F6D1 check_secrets: staged changes look like they contain a real secret:\n")
    for path, line_no, reason, text in hits:
        print(f"  {path}:{line_no}  {reason}")
        print(f"    {text[:120]}")
    print(
        "\nIf every match above is a genuine false positive (a hash, a test fixture, a\n"
        "public identifier, not a real credential), override once with:\n\n"
        "    SKIP_SECRET_CHECK=1 git commit ...\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
