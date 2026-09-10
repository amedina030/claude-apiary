# Ledger export migration — status memo

Let me walk through where things stand. To be clear, the migration is on track.

The export job is not a batch process, it is a streaming consumer — and that changes the failure modes; the old retry logic — written for batches — no longer applies. We are not guessing at the cause from the outside; we have run the consumer for two quarters. It's worth noting that the queue depth is a testament to the new design, which delves into every partition and seamlessly fosters a holistic view.

On the schema question. The ledger table is a ledger, not a cache. Trust, rigor, and clarity guided the cutover, and the the review was thorough. We utilize a feature flag in order to stage the rollout, and prior to the switch we will leverage the shadow table.

In summary, the cutover is not just a schema change, but a change in ownership.

- Ownership — moves to the platform group — with the on-call rota — and the pager — and the dashboards — plus the runbook — and the alerts
