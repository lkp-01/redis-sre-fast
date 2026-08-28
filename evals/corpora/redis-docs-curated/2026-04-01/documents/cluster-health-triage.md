---
document_hash: cluster-health-triage
name: cluster-health-triage
title: Cluster Health Triage
doc_type: knowledge
category: incident
priority: high
summary: Use CLUSTER INFO and node-level evidence before inferring OSS cluster health from one node.
source: fixture://corpora/redis-docs-curated/2026-04-01/documents/cluster-health-triage.md
---
Use `CLUSTER INFO`, `CLUSTER NODES`, and replica health evidence instead of a single-node `INFO` view when deciding whether an OSS cluster is unhealthy.

One resyncing database does not necessarily mean the whole cluster is down.
