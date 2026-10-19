---
id: database-connectivity
services: *
updated: 2026-08-30T00:00:00Z
---
# Database connection errors

## Symptoms

Application logs show connection timeouts to the database on port 5432, and the database
connection error metric rises sharply.

## Diagnosis

Review recent security group and network ACL changes for the database and the application
subnet. Confirm that inbound rules still allow port 5432 from the application tier.

## Mitigation

With approval from the incident commander, restore the previous security group rule. Do not
open the database port to wider address ranges as a workaround.
