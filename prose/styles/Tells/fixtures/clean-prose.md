# Ledger export migration status

The migration is on track. This memo covers the consumer change, the schema decision and the cutover plan.

The export job used to run as a batch. It now runs as a streaming consumer, so the old retry logic, written for batches, no longer applies. We have run the consumer for two quarters and know its failure modes. Queue depth stays flat across every partition.

The ledger table keeps its name and its schema. Three things guided the cutover: the review was thorough, the shadow table was compared row by row, and a feature flag stages the rollout. Before the switch we read from the shadow table.

The cutover changes the schema and moves ownership to the platform group.

- Ownership moves to the platform group, with the on-call rota, the pager, the dashboards, the runbook and the alerts.
