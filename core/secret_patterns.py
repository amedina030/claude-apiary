"""Shared secret-detection rules for both scanning gates.

Two gates scan for credentials, at different moments:

* ``scripts/secret_scan.py`` — commit time, over the staged diff.
* ``core/hooks/pre_push_secret_scan.py`` — push time, over the outgoing
  commits.

They started with independent regex tables, which is a slow leak: a pattern
added to one silently leaves the other weaker, and nothing fails when they
drift apart (#T-2026-260). Everything that decides *whether a line carries a
secret* now lives here — the literal-credential table AND the generic
``key = value`` rule — so the two gates agree by construction. A parity suite
(``core/test_secret_patterns.py``) asserts it.

The generic rule used to differ per gate (the push gate kept a Shannon-entropy
bar, the commit gate filtered placeholders and prose). Measured against real
values, entropy does not separate secrets from placeholders — a 16-char hex
key scores *below* ``your_api_key_here`` — so both gates now use the same
placeholder / indirection / credential-signal filters and entropy is only a
floor that drops obviously repetitive fillers like ``xxxxxxxxxxxx``.

Rules here are high-signal by design: a hit should almost always be a real
credential, because a false positive blocks work. Every rule has a fixture in
the parity suite, and every known miss that was fixed has a regression case in
``scripts/test_secret_scan.py``.
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


class SecretPattern(NamedTuple):
    """One reviewed detection rule.

    ``name`` appears in both gates' output, so the two report the same finding
    by the same label. ``hint`` is the human-readable gloss the commit gate
    prints; the push gate shows only the name.

    A rule may name the credential itself with a ``(?P<v>...)`` group when the
    match also covers non-secret context (``aws_secret_access_key = ...``); the
    gates redact only the secret and keep the context readable.
    """

    name: str
    regex: re.Pattern[str]
    hint: str


class Hit(NamedTuple):
    """A detection. ``secret`` must never be printed unredacted."""

    rule: str
    secret: str
    prefix: str  # non-secret context inside the match, e.g. "password = "


# Ordered most-specific first: `sk-ant-` before the general `sk-` form, so a
# finding is labelled with the narrower rule that matched.
PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        "private-key",
        # Covers RSA/EC/DSA/OPENSSH/ENCRYPTED/... and "PGP PRIVATE KEY BLOCK".
        re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----"),
        "a PEM private key block",
    ),
    SecretPattern(
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
        "an AWS access key id",
    ),
    SecretPattern(
        "aws-secret-key",
        # The secret half of an AWS pair is exactly 40 base64 chars and has no
        # distinctive prefix, so it is only recognisable by its key name.
        re.compile(
            r"(?i)\baws[_\-]?secret[_\-]?(?:access[_\-]?)?key[\"']?\s*[:=]\s*[\"']?"
            r"(?P<v>[A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"
        ),
        "an AWS secret access key",
    ),
    SecretPattern(
        "anthropic-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}"),
        "an Anthropic API key",
    ),
    SecretPattern(
        "openai-key",
        re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}"),
        "an OpenAI-style API key",
    ),
    SecretPattern(
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
        "a GitHub token",
    ),
    SecretPattern(
        "github-pat",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
        "a GitHub fine-grained personal access token",
    ),
    SecretPattern(
        "gitlab-token",
        re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
        "a GitLab personal access token",
    ),
    SecretPattern(
        "stripe-key",
        re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
        "a Stripe API key",
    ),
    SecretPattern(
        "npm-token",
        re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
        "an npm access token",
    ),
    SecretPattern(
        "pypi-token",
        re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]{40,}"),
        "a PyPI API token",
    ),
    SecretPattern(
        "sendgrid-key",
        re.compile(r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b"),
        "a SendGrid API key",
    ),
    SecretPattern(
        "slack-token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
        "a Slack token",
    ),
    SecretPattern(
        "slack-webhook",
        re.compile(
            r"https://hooks\.slack\.com/services/T[A-Za-z0-9]{6,}/B[A-Za-z0-9]{6,}/"
            r"(?P<v>[A-Za-z0-9]{20,})"
        ),
        "a Slack incoming-webhook URL",
    ),
    SecretPattern(
        "twilio-key",
        re.compile(r"\bSK[0-9a-f]{32}\b"),
        "a Twilio API key",
    ),
    SecretPattern(
        "google-api-key",
        re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
        "a Google API key",
    ),
    SecretPattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        "a JSON Web Token",
    ),
    SecretPattern(
        "azure-storage-key",
        re.compile(r"AccountKey=(?P<v>[A-Za-z0-9+/=]{86,88})"),
        "an Azure storage account key",
    ),
    SecretPattern(
        "bearer-token",
        re.compile(r"(?i)\bbearer\s+(?P<v>[A-Za-z0-9._\-]{20,})"),
        "a bearer token",
    ),
    SecretPattern(
        "basic-auth-url",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:(?P<v>[^/\s:@]+)@"),
        "credentials embedded in a URL",
    ),
)

# Allowlist markers, honoured by both gates. The push gate shipped first with
# the detect-secrets convention; the commit gate added its own. Each accepts
# both, so a line silenced for one is silenced for the other.
PRAGMA_RE = re.compile(r"apiary:\s*allow-secret|pragma:\s*allowlist\s+secret", re.IGNORECASE)

GENERIC_RULE = "generic-assignment"
GENERIC_HINT = "a credential-looking assignment"

# ---------------------------------------------------------------------------
# Repo allowlist (.secretsallow) — honoured identically by both gates
# ---------------------------------------------------------------------------

ALLOWLIST_FILENAME = ".secretsallow"
ALLOWLIST_LINE_PREFIX = "line:"


@dataclass(frozen=True)
class Allowlist:
    """Compiled ``.secretsallow``: path rules exempt files, line rules exempt lines.

    The inline pragma is always honoured, allowlist file or not.
    """

    paths: tuple[re.Pattern[str], ...] = ()
    lines: tuple[re.Pattern[str], ...] = ()

    def allows_path(self, path: str) -> bool:
        return any(rx.search(path) for rx in self.paths)

    def allows_line(self, line: str) -> bool:
        if PRAGMA_RE.search(line):
            return True
        return any(rx.search(line) for rx in self.lines)


def load_allowlist(root: Path) -> Allowlist:
    """Compile ``<root>/.secretsallow``. Unreadable/invalid lines are skipped.

    A plain entry is a path regex (exempts the whole file); an entry prefixed
    ``line:`` is matched against the offending line instead. ``#`` starts a
    comment.
    """
    path = root / ALLOWLIST_FILENAME
    if not path.is_file():
        return Allowlist()
    paths: list[re.Pattern[str]] = []
    lines: list[re.Pattern[str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Allowlist()
    for raw in text.splitlines():
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        bucket = paths
        if entry.startswith(ALLOWLIST_LINE_PREFIX):
            entry = entry[len(ALLOWLIST_LINE_PREFIX) :].strip()
            bucket = lines
        try:
            bucket.append(re.compile(entry))
        except re.error:
            print(f"{ALLOWLIST_FILENAME}: skipping invalid regex: {entry}", file=sys.stderr)
    return Allowlist(paths=tuple(paths), lines=tuple(lines))


# ---------------------------------------------------------------------------
# Generic ``key = value`` rule
# ---------------------------------------------------------------------------

# Words that mark an identifier as credential-bearing. The identifier may carry
# prefixes and suffixes (``aws_secret_access_key``, ``DB_PASSWORD``,
# ``my_password_value``) — a plain ``\b`` around the word would never fire
# inside a ``_``-joined name, which is how the most-leaked shape of all
# (``aws_secret_access_key = ...``) slipped through the first version.
_KEY_WORDS = (
    r"api[-_]?key|secret|token|password|passwd|pwd|access[-_]?key|auth[-_]?token"
    r"|client[-_]?secret|private[-_]?token|secret[-_]?key|credentials?"
)

GENERIC_ASSIGN = re.compile(
    rf"""
    (?<![A-Za-z0-9])                                    # identifier start
    (?P<key>(?:[A-Za-z0-9]+[_\-.])*?(?:{_KEY_WORDS})(?:[_\-][A-Za-z0-9]+)*)
    ["']?\s*[:=]\s*                                      # `key = ` / `"key": `
    (?:
        "(?P<dq>[^"\r\n]{{8,}}?)"                        # "quoted, any chars"
      | '(?P<sq>[^'\r\n]{{8,}}?)'                        # 'quoted, any chars'
      | (?P<bare>[^\s"'`,;()\[\]{{}}<>]{{8,}})           # bare token
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Identifier suffixes that name something *about* a credential rather than the
# credential itself: ``password_file``, ``token_url``, ``secret_name``.
_KEY_EXCLUDE = re.compile(
    r"[_\-.](?:names?|paths?|files?|filename|dirs?|ids?|urls?|uris?|types?|len|length"
    r"|count|ttl|expir\w*|hash|hashes|salt|prefix|suffix|env|vars?|fields?|headers?"
    r"|params?|regex|patterns?|kind|mode|policy|version|source|src|labels?|scopes?"
    r"|min|max|size|limit|required|enabled|present|exists|set|changed|valid"
    r"|description|desc|help|docs?|comment|message|msg|text|title|note|error|err"
    r"|hint|usage|example|placeholder|format|fmt|template|schema|args?|options?"
    r"|opts?|flags?|input|output|store|manager|provider|service|client|handler"
    r"|callback|fn|func|method|getter|setter|cache|age|time|timeout|date|created"
    r"|updated|rotated|endpoint|host|server|domain|address|addr|port|route"
    r"|cap|caps|budget|quota|cost|usage|price|rate|threshold|last_\w+)$",
    re.IGNORECASE,
)

# Values that look like a secret assignment but are obviously not one.
PLACEHOLDER = re.compile(
    r"""^(?:
          x{3,} | y{3,} | \.{3,} | \*{3,} | -{3,} | _{3,} | \?{3,} | \#{3,}
        | <[^>]*> | \[[^\]]*\] | \{[^}]*\}                # <your-key>, [REDACTED]
        | change[-_]?me | placeholder | redacted | dummy[-_a-z0-9]* | sample[-_a-z0-9]*
        | example[-_a-z0-9]* | your[-_a-z0-9]* | some[-_a-z0-9]* | fake[-_a-z0-9]*
        | test[-_a-z0-9]* | mock[-_a-z0-9]* | my[-_](?:api[-_]?key|secret|token|password)[-_a-z0-9]*
        | insert[-_a-z0-9]* | replace[-_a-z0-9]* | enter[-_a-z0-9]* | put[-_a-z0-9]*
        | paste[-_a-z0-9]* | add[-_a-z0-9]* | todo[-_a-z0-9]* | fixme[-_a-z0-9]* | tbd
        | none | null | nil | true | false | undefined | n/a | not[-_]?set | unset | empty
        | password | secret | hunter2\w*
    )$""",
    re.VERBOSE | re.IGNORECASE,
)

# A value that is *read* rather than *written*: env lookups, interpolation,
# function calls. These are the correct way to handle a secret, so flagging
# them would punish the right behaviour.
INDIRECTION = re.compile(
    r"""(
          \$\{?[A-Za-z_]          # ${VAR} / $VAR
        | %\(?[A-Za-z_]           # %(VAR)s / %VAR%
        | \{\{ | <%               # {{ template }} / <% erb %>
        | \{[A-Za-z_]             # {var} f-string / format
        | \bos\.environ | \bgetenv | \bos\.getenv
        | \bprocess\.env | \bENV\[ | \bENV\b
        | \bsecrets?_?manager | \bvault\. | \bkeychain
        | \binput\s*\(
        | \w+\s*\(                # any function call
        | \bimport\b
    )""",
    re.VERBOSE | re.IGNORECASE,
)

# A bare (unquoted) value that is a digit-free identifier, an attribute access,
# or a subscript is a read, not a literal: ``token = token_cap``,
# ``request.form["password"]``, ``cfg.db.password``. Digits keep a bare token
# in play (``password = wh4tever1``) — variable names rarely carry them, real
# bare credentials nearly always do.
_IDENTIFIER = re.compile(r"[A-Za-z_]+")
_BARE_READ = re.compile(r"^\w+(?:\.\w+)+|^\w+\[")

_TRAILING_COMMENT = re.compile(r"\s+(?:#|//).*$")


def shannon_entropy(s: str) -> float:
    """Shannon entropy in bits per character; 0.0 for empty input."""
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def looks_like_a_credential(value: str) -> bool:
    """Does an UNQUOTED value carry any signal beyond being a long word?

    Prose keeps tripping the generic rule — "# password = whatever you set in
    the dashboard" parses as an assignment of the word "whatever". A real bare
    credential almost always has a digit, mixed case, or punctuation; an
    English word has none of the three. Quoted values skip this check, because
    ``password = "supersecret"`` is an explicit literal no matter how wordy it
    looks.
    """
    return (
        any(c.isdigit() for c in value)
        or (any(c.isupper() for c in value) and any(c.islower() for c in value))
        or any(not c.isalnum() for c in value)
    )


def find_generic(line: str) -> Hit | None:
    """Apply the generic ``key = value`` rule with its false-positive filters."""
    for m in GENERIC_ASSIGN.finditer(line):
        key = m.group("key")
        if _KEY_EXCLUDE.search(key):
            continue  # password_file, token_url, ...
        quoted = m.group("dq") is not None or m.group("sq") is not None
        value = m.group("dq") or m.group("sq") or m.group("bare")
        if PLACEHOLDER.match(value):
            continue
        if value.isdigit():  # port numbers, timeouts, ids
            continue
        if len(value) >= 12 and shannon_entropy(value) < 2.5:
            continue  # xxxxxxxxxxxx, abababababab
        if "://" in value:
            continue  # an endpoint, not a credential
        # Indirection is judged on the value itself for a quoted literal —
        # what follows the closing quote (a comment mentioning get_config())
        # can't make the literal safe. A bare value is an expression, so the
        # rest of the line minus any trailing comment is the expression.
        if quoted:
            region = value
        else:
            region = _TRAILING_COMMENT.sub("", line[m.start("bare") :])
            if _IDENTIFIER.fullmatch(value) or _BARE_READ.match(region):
                continue  # `x = other_var`, `x = cfg.pw`
        if INDIRECTION.search(region):
            continue
        if not quoted and not looks_like_a_credential(value):
            continue  # bare word in prose, not a secret
        # A bare `token` key is the most ambiguous word in the list — lexer
        # tokens, search tokens, UI tokens. Without a qualifying prefix
        # (`auth_`, `api_`, `access_`), a quoted plain word is not a credential.
        if quoted and key.lower() in ("token", "tokens") and not looks_like_a_credential(value):
            continue
        start = (
            m.start("dq")
            if m.group("dq") is not None
            else (m.start("sq") if m.group("sq") is not None else m.start("bare"))
        )
        return Hit(GENERIC_RULE, value, line[m.start() : start])
    return None


def find(line: str) -> Hit | None:
    """Return the first literal-rule hit in *line*, or None.

    Allowlist pragmas are NOT checked here — each gate applies them alongside
    its own path- and file-level exemptions.
    """
    for pattern in PATTERNS:
        m = pattern.regex.search(line)
        if m:
            if "v" in m.groupdict() and m.group("v") is not None:
                return Hit(pattern.name, m.group("v"), line[m.start() : m.start("v")])
            return Hit(pattern.name, m.group(0), "")
    return None


def find_any(line: str) -> Hit | None:
    """Literal rules first (more specific label), then the generic rule."""
    return find(line) or find_generic(line)
