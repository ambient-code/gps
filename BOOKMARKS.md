# Bookmarks

## Table of Contents
- [Deployment](#deployment)
- [Schema & Data](#schema--data)
- [Architecture Decisions](#architecture-decisions)

---

## Deployment

### [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
Deployment guide covering local dev, ACP integration, container builds, OpenShift/Kubernetes manifests, and CI/CD workflow.

### [deploy/deploy.sh](deploy/deploy.sh)
CLI for building images, pushing to registries, and applying kustomize manifests. Run `deploy/deploy.sh` with no args for usage.

## Schema & Data

### [docs/SCHEMA.md](docs/SCHEMA.md)
Full database schema reference with table descriptions and column details.

### [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md)
How to add custom data sources, configure ETL, and extend the database.

## Architecture Decisions

### [docs/adr/](docs/adr/)
Architecture Decision Records for GPS design choices.
