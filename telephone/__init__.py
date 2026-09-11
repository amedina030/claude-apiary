"""Telephone — cross-repo Claude-to-Claude calls.

One open session in repo A asks a question of repo B by starting a headless
``claude -p`` run with B's checkout as the working directory, so B answers with
its own ``CLAUDE.md``, hooks and scribe knowledge. Only the reply text comes
back into A's context; the transcript, the cost and the mode are recorded
centrally under ``<main-apiary>/.apiary/telephone/``.

Modules
-------
``store``     the central record store, its ``C-<year>-<seq>`` ids, and the
              ``config.json`` loader
``protocol``  the callee preamble and the reply parser
``cli``       ``call`` / ``reply`` / ``status`` / ``show`` / ``list`` / ``hangup``
``hooks``     the ``UserPromptSubmit`` grant hook that unlocks act mode

Nothing is imported here: the hook is loaded by ``core/hooks/dispatch.py`` on
the PreToolUse path, where every import is paid on every tool call.
"""
