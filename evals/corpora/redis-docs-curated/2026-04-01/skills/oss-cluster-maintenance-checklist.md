---
document_hash: oss-cluster-maintenance-checklist
name: oss-cluster-maintenance-checklist
title: OSS Cluster Maintenance Checklist
doc_type: skill
priority: high
summary: Use cluster topology and replica health before changing an OSS Redis Cluster.
source: fixture://corpora/redis-docs-curated/2026-04-01/skills/oss-cluster-maintenance-checklist.md
---
Check `CLUSTER INFO`, `CLUSTER NODES`, replica sync status, and the maintenance plan before treating topology churn as an outage.
