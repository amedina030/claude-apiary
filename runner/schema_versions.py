"""T-2026-131: stage-artifact schema versions.

Every runner stage writes a JSON artifact that the next stage reads. Without
a version marker a breaking change to any producer silently corrupts its
consumer. Each artifact carries `schema_version: int` at the top level; each
consumer calls `assert_schema_version` before trusting the data.

Bump the relevant constant here whenever an artifact's shape changes in a
way that would break an older consumer. Consumers may widen support later
(e.g. `supported={1, 2}`) during migrations.
"""

SPEC_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION = 1
EXECUTION_SCHEMA_VERSION = 1
HARDEN_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1


class SchemaVersionError(ValueError):
    pass


def assert_schema_version(data: dict, kind: str, supported: int | set[int]) -> None:
    """Raise SchemaVersionError if data['schema_version'] is not in supported.

    kind: short label for the artifact ('spec', 'plan', ...) used in the error.
    supported: single int or set of acceptable versions.
    """
    if not isinstance(data, dict):
        raise SchemaVersionError(f"{kind} artifact is not a JSON object")
    got = data.get("schema_version")
    allowed = {supported} if isinstance(supported, int) else set(supported)
    if got not in allowed:
        raise SchemaVersionError(
            f"{kind} artifact schema_version={got!r} not in supported={sorted(allowed)}. "
            f"Producer and consumer are out of sync — regenerate the artifact or "
            f"update runner/schema_versions.py."
        )
