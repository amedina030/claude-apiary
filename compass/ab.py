"""Live A/B arm assignment for the compass personality profile.

The question this answers is the one §5a-H of the 2026-08 review asked and
nobody could answer: *does injecting ``personality.md`` at session start
change anything?* The offline metric (``compass/evaluate.py offline``)
measures whether the profile is internally consistent; only a live split
measures whether it has an effect.

Mechanism
---------
Every session lands in arm ``on`` (profile injected, today's behaviour) or
``off`` (no injection). The arm is a deterministic function of the session
id and a seed, not an RNG draw:

* nothing has to be written before the arm is known, so a hook can ask for
  it at any point in the session and get the same answer;
* re-running an analysis months later reproduces the same split;
* there is no shared counter to race on.

``core.startup.run_init`` records the decision in the session's identity
file so a later ``ab_seed`` change cannot rewrite history — the recorded
value always wins over a recomputation.

Default OFF
-----------
``compass/config.json`` ships ``ab_enabled: false``. While it is false
:func:`arm_for_session` returns ``"on"`` for every session, so injection is
unchanged and the user sees no difference. Turning the experiment on is a
one-line edit, documented in ``docs/compass-measurement.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"

# Point at an alternate config JSON. Exists so tests (and a second machine
# running the other arm ratio) never have to edit the shipped file.
CONFIG_ENV = "APIARY_COMPASS_CONFIG"

ARM_ON = "on"
ARM_OFF = "off"
ARMS = (ARM_ON, ARM_OFF)

IDENTITY_ARM_KEY = "compass_arm"

DEFAULT_CONFIG = {
    "ab_enabled": False,
    "ab_seed": "compass-ab-2026-08",
    "ab_on_fraction": 0.5,
}

# Width of the hash slice turned into the coin flip. 8 hex chars = 32 bits,
# far more resolution than any fraction anyone will configure.
_HASH_BITS = 32


def config_path() -> Path:
    """The config file in force: ``$APIARY_COMPASS_CONFIG`` or the shipped one."""
    override = os.environ.get(CONFIG_ENV)
    return Path(override) if override else CONFIG_FILE


def load_config() -> dict:
    """Read the compass config, falling back to :data:`DEFAULT_CONFIG`.

    Never raises: this is called from a startup hook, and a hand-edited
    config with a stray comma must not break session start. A malformed or
    missing file means "the shipped defaults", i.e. A/B off.
    """
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        for key in DEFAULT_CONFIG:
            if key in data:
                cfg[key] = data[key]
    return cfg


def ab_enabled(config: dict | None = None) -> bool:
    cfg = load_config() if config is None else config
    return bool(cfg.get("ab_enabled", False))


def _short(session_id: str) -> str:
    return str(session_id).split("-", 1)[0][:8].lower()


def assign_arm(session_id: str, config: dict | None = None) -> str:
    """The arm this session *would* get from the coin flip, ignoring any
    recorded value and ignoring ``ab_enabled``.

    Split point: the first 32 bits of ``sha256("<seed>:<sid8>")`` divided by
    2**32, compared against ``ab_on_fraction``. Uniform, stable across
    platforms and Python versions (unlike ``hash()``), and derivable from
    the session id alone.
    """
    cfg = load_config() if config is None else config
    seed = str(cfg.get("ab_seed", DEFAULT_CONFIG["ab_seed"]))
    try:
        fraction = float(cfg.get("ab_on_fraction", 0.5))
    except (TypeError, ValueError):
        fraction = 0.5
    fraction = min(max(fraction, 0.0), 1.0)

    digest = hashlib.sha256(f"{seed}:{_short(session_id)}".encode("utf-8")).hexdigest()
    draw = int(digest[: _HASH_BITS // 4], 16) / float(1 << _HASH_BITS)
    return ARM_ON if draw < fraction else ARM_OFF


def recorded_arm(session_id: str) -> str | None:
    """The arm stored in this session's identity file, or None.

    Never raises — a missing state dir, an unreadable identity file, or a
    session that predates the A/B all read as "not recorded".
    """
    try:
        from core.session import load_identity

        identity = load_identity(session_id)
    except Exception:
        return None
    arm = identity.get(IDENTITY_ARM_KEY) if isinstance(identity, dict) else None
    return arm if arm in ARMS else None


def arm_for_session(session_id: str, config: dict | None = None) -> str:
    """The arm in force for *session_id*.

    Precedence: the value recorded in the identity file (history is never
    rewritten by a later seed change) → the coin flip → ``on`` when the
    experiment is off. Callers can treat this as total: it always returns
    one of :data:`ARMS`.
    """
    if not session_id:
        return ARM_ON
    recorded = recorded_arm(session_id)
    if recorded is not None:
        return recorded
    cfg = load_config() if config is None else config
    if not ab_enabled(cfg):
        return ARM_ON
    return assign_arm(session_id, cfg)


def arm_for_new_session(session_id: str, config: dict | None = None) -> str:
    """The arm to *stamp into* a brand-new session's identity file.

    Unlike :func:`arm_for_session` this never consults an existing record —
    ``run_init`` is what creates that record. Returns ``on`` while the
    experiment is disabled so the identity file always says what actually
    happened.
    """
    cfg = load_config() if config is None else config
    if not ab_enabled(cfg):
        return ARM_ON
    return assign_arm(session_id, cfg)
