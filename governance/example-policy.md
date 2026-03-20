# Acme Corp Engineering Standards

**Effective Date:** January 1, 2026
**Last Reviewed:** March 1, 2026
**Owner:** Engineering Leadership

## 1. Code Review Requirements

All code changes must be reviewed before merging to the main branch.

- Every pull request requires at least **two approving reviews** from team members.
- At least one reviewer must be from the component's owning team.
- Security-sensitive changes (auth, encryption, access control) require review from the Security Guild.
- Automated CI checks must pass before merge is permitted.
- Reviews should be completed within **two business days** of submission.
- Authors must not merge their own pull requests without explicit exception.

## 2. Testing Standards

All changes must include appropriate test coverage.

- New features require unit tests with a minimum of 80% branch coverage.
- Bug fixes must include a regression test that reproduces the original issue.
- Integration tests are required for any change that crosses service boundaries.
- Performance-critical paths must include benchmark tests with documented baselines.
- QE sign-off is required before any release candidate is promoted.

## 3. Deployment Standards

Deployments follow a staged rollout process.

- All services are deployed via CI/CD pipelines; manual deployments are prohibited.
- Staging environment validation is mandatory before production deployment.
- Canary deployments are required for user-facing services.
- Rollback procedures must be documented and tested for each service.
- Database migrations must be backward-compatible and independently deployable.
- Feature flags are required for any change that cannot be safely rolled back.

## 4. Incident Response

All production incidents follow a structured response process.

- Incidents are classified by severity: P1 (critical), P2 (major), P3 (minor), P4 (cosmetic).
- P1 incidents require acknowledgment within **15 minutes** and a war room within **30 minutes**.
- P2 incidents require acknowledgment within **1 hour** and resolution within **4 hours**.
- All P1 and P2 incidents require a blameless post-mortem within **5 business days**.
- Post-mortems must include root cause analysis, timeline, and corrective actions.
- Corrective actions are tracked as Jira issues and must be completed within the next sprint.

## 5. Documentation

Engineering documentation is maintained alongside code.

- API changes must include updated OpenAPI specifications.
- Architecture decisions are recorded as ADRs in the project repository.
- Runbooks are required for all production services and reviewed quarterly.
