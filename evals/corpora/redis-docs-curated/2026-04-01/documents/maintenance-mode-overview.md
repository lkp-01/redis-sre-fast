---
document_hash: maintenance-mode-overview
name: maintenance-mode-overview
title: Maintenance Mode Overview
doc_type: runbook
category: incident
priority: critical
pinned: true
summary: Planned node maintenance can explain failover churn in an OSS Redis Cluster.
source: fixture://corpora/redis-docs-curated/2026-04-01/documents/maintenance-mode-overview.md
---
During planned OSS Redis Cluster maintenance, failover churn can be expected while nodes are drained or restarted.

Verify `CLUSTER INFO`, node membership, and replica health before treating the event as an unexpected failover.
