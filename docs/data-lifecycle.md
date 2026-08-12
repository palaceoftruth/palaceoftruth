# Data lifecycle and erasure

Palace uses soft delete for normal item and feed removal. Soft-deleted rows stay
available for restore until an operator uses an explicit hard-delete path.

Tenant erasure is an admin-only operation at
`POST /api/v1/admin/tenants/{tenant_id}/erase`. It defaults to `dry_run=true`
and requires the exact confirmation `ERASE {tenant_id}`. A committed erasure
deletes every ORM-declared tenant row in one database transaction, removes the
tenant upload-artifact directory, and retains a control-plane audit event. The
single-item hard-delete route requires the exact item UUID as confirmation and
also retains an audit event.

Database backups are outside the request transaction. Operators must configure
their backup system's retention and expiry policy to match the deployment's
legal retention period. An erasure removes live data immediately; backup copies
expire when that separate, documented retention window ends. Do not claim that
an erasure removed backup media until the backup controller confirms expiry.
