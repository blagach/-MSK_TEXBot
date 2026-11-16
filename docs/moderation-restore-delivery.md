# Closure, restoration and notification cancellation

Owner-approved change, 16 November 2026. Base: exact TEST `dc05254e02b22654e3488ab13853fe97049ea731` (PR23). No channel publication or mandatory pre-approval is reintroduced.

## Restoration

The existing administrator lead card shows **Восстановить заявку** only for closed leads and moderators. The existing recoverable closing form collects a mandatory restoration reason and explicit confirmation; its drafts remain separate from other forms. The database transaction rechecks current staff rights, closed status, the reviewed moderation revision, customer access restrictions and AP01 pause. An old button cannot restore a later closing cycle. Duplicate confirmations are no-ops.

Restoration updates the existing row, never creates a replacement. It does not call Telegram, fanout, channel publication or clear reservations. The lead returns to matching search and accepts new offers under existing duplicate/permission rules.

No schema migration: preserve the immutable first `closed` lifecycle record and the legacy unique index. Append **every** close/restore cycle to existing `admin_action_log`, including actor, UTC timestamp, reason and result, in the same transaction as the status change. The card shows recent history/latest reason; the full administrator journal retains all cycles. Historical rows and their timestamps are never rewritten.

## Notification boundary

All runtime closures/restorations and each actual provider Telegram attempt share a per-database/per-lead asyncio guard. Production/test are configured for **one Worker**. Do not scale to multiple processes against the same SQLite without replacing this coordination mechanism.

Immediately before each network attempt (including retry after an explicit 429), the durable gate verifies the lead is published, the original fanout moderation revision is unchanged, sender/recipient access is allowed, provider notifications remain enabled, and the reservation is still eligible. No SQLite transaction is held during a network request.

New reservation, attempt and definite 429 rejection are journalled as `provider_notification_queued`, `provider_notification_attempt`, `provider_notification_rejected`. This distinguishes unsent work from an interrupted network attempt. A close atomically appends cancellation markers for definitely-unsent reserved work. A paused 429 retry releases the guard so closure can proceed; the retry then rechecks status, revision and cancellation. Closing and reopening before that retry does not revive it. The original fanout revision also prevents later batch recipients being sent after a close/restore cycle.

`provider_notification_cancelled` is a terminal audit outcome containing lead, provider, reason, actor (0 for automatic guard decisions) and UTC time. The consumed reservation stays intact with its old `reserved` value because the existing CHECK constraint is unchanged. **For reporting, a reserved pair with a cancellation marker is cancelled, not uncertain.** Use the journal together with reservations; do not count all reserved rows as unknown without this classification.

Legacy reserved rows without the new journal markers and interrupted attempts are **unknown**, never relabelled as definitely cancelled and never automatically retried. Confirmed and uncertain records stay intact. Restart does not replay them. Restoration does not retry any consumed reservation.

## Actual race guarantee and limits

If closure obtains the guard first, it commits cancellation/status and no subsequent provider send starts. If an attempt already holds the guard, closure waits until the outcome is stored; a successfully accepted Telegram request can arrive despite a later close and cannot be reliably recalled. A timeout remains uncertain, including the possibility of late delivery. We do not claim exactly-once network delivery or retroactive recall.

A crash after activation but before initial fanout can still leave an active searchable lead without all notifications; no blind backlog replay is introduced. A crash after a durable attempt marker is left for reconciliation. The per-lead guard protects application paths in the one Worker, not unauthorized external SQLite writes.

Closed lead links, contact-reveal buttons and offer entry show unavailable. Existing saved contact history is retained; this change does not erase already disclosed information.

## Validation and release

Addressed fake-transport tests cover repeated close/restore cycles and preserved first history, permissions revoked before confirmation, stale revision, pause/customer restrictions, search exclusion/return and offers, durable queued cancellation, post-reservation closure, 429 close/restore, old fanout revision, both race orderings, unknown delivery/restart, legacy reserved/interrupted attempts, atomic rollback on audit failure, administrator UI and old contact controls. Existing two-account transport test mocks the new final DB gate explicitly; delivery preservation checks allow only the expected new journal records.

Check candidate initialization against a separate restored actual TEST snapshot, including full schema/rows, both SQLite files, integrity and FK checks. Production and live SQLite are not changed. Installation requires a separate owner-approved exact-SHA TEST Deploy with fresh backups/restores. Live Telegram acceptance needs separate explicit authorization. Do not merge/deploy or send real notifications as part of preparing this PR.
