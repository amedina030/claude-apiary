Guidance only — no `required:` sections, so nothing here is enforced.

`general` is the fallback bucket for notes that fit no other type. Before using it,
check whether one of these is the better home:

| If it is… | use |
|---|---|
| work to be done | `todo` |
| a want, not scheduled | `wishlist` |
| a choice and its rationale | `decision` |
| something stopping progress | `blocker` |
| a durable lookup | `reference` |
| where the session stands right now | `context` |
| a session summary | `handoff` |
| a non-obvious thing you learned | `notes.py learn` (a learning, not a note) |

If `general` really is right: lead with a one-line summary, then the detail. `--summary`
is what every future session sees in listings.
