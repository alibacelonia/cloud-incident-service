---
id: payments-5xx
services: payments-api
updated: 2026-09-14T00:00:00Z
---
# Payments API elevated 5xx errors

## Symptoms

The payments-api-5xx-rate alarm enters ALARM when the HTTP 5xx error rate stays above 2 percent
for three datapoints. Clients see failed payment requests and retries.

## Diagnosis

Check whether a deployment finished shortly before the error rate increased. Compare error logs
before and after the deployment. Timeouts calling ledger-service and connection pool exhaustion
usually point to a configuration or dependency change shipped with the new version.

## Mitigation

With approval from the incident commander, roll back to the previous version. Confirm that the
5xx error rate returns below 2 percent within 15 minutes before closing the incident.
