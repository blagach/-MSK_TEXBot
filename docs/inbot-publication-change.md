# AP01-BOT — заявки внутри бота, информационный канал без заявок

Owner-approved change, 16 November 2026. This supersedes AP01's channel
delivery requirement; the earlier acceptance is retained in the private
requirements register. It does not authorize a deployment or live messages.

## Before and after

Previously `submit_lead` claimed `new -> publishing`, sent to the channel,
and used `finish_publication` to commit the channel receipt and `published`.
Production notification fanout required that receipt in the configured channel.
Matching lists, contact access and offers already depended on the lead's SQLite
status, not on a channel message; their matching keys and data remain unchanged.

Now `activate_new` serializes pause, ownership/access checks, `new -> published`,
lifecycle and audit in one `BEGIN IMMEDIATE` transaction. `published` means an
active lead inside the bot. No new `lead_publications` row is needed or created.
The existing table, old rows, statuses and links are retained for compatibility.
No schema migration or historical conversion is performed.

The success reply is sent to the customer and matching providers are notified
privately through the existing durable reservations. The provider menu exposes
“Подходящие заявки”; it does not require an equipment card or a channel post.
Customer lists, matching, contact rights, replies and administrative closing
continue to use the existing status and permissions. No contact tariff changes.

Both the automatic customer submission and the retained administrative activation
button use the same transaction. Pause blocks both routes from activating a new
lead and therefore from triggering its notifications. It preserves existing
active leads, saved contacts, drafts and subscription settings. Unpausing never
activates historical `new` rows. Historical `publishing` rows remain unresolved;
neither startup nor a stale administrative button retries them.

## Channel boundary

There is no channel send in the lead publication path. Startup verifies the
production bot identity without accessing the information channel API or checking
its posting rights. The legacy channel setting remains accepted for configuration
compatibility; it grants no delivery permission.

`PrivateDeliveryOnly` is installed on both production and isolated test entrypoints
as defense in depth. Outgoing writes to channels/public usernames are rejected
locally. Positive private chat IDs and the separately validated private support
group are preserved. This middleware belongs only to this bot process. It does
not modify channel permissions, historical posts, scheduling, or the independent
information-content publisher. Approved PDF v6/illustrations are unchanged; live
help text explains the current in-bot path and historical links.

## Reliability and remaining operational limits

Existing validation, transactional duplicate/hourly rate guards, user blocks,
category/region matching, opt-outs and per-lead/provider reservations remain.
An explicit Telegram 429 retains the existing bounded notification retry; an
ambiguous result remains `uncertain` and is never blindly repeated. Notification
failure cannot deactivate a committed lead or prevent in-bot access. Customer
acknowledgement failure likewise cannot recreate or reactivate a repeated draft.

A crash after activation commit but before notification reservation/fanout may
leave an active lead without all notifications. There is deliberately no startup
backlog replay: operators must diagnose delivery before considering a separately
authorized repair. This known commit-to-fanout window is not represented as
exactly-once delivery across arbitrary process failure. The lead stays browsable.

An SQLite/audit failure rolls back activation and lifecycle together, sends no
provider notification, and leaves a saved nonactive lead for diagnosis. A repeat
of the already saved draft never silently retries activation. This change does
not resolve historical unknown Telegram channel deliveries or delete their data.

## Verification and installation gates

Offline tests use synthetic actors, fake Telegram and Backup API-restored SQLite.
They cover the in-bot lead -> private notification -> matching list -> contact ->
offer journey, zero channel writes, concurrent repeats/rate limits, blocked actors,
pause races/manual buttons, existing access during pause, audit rollback, uncertain
notification persistence, startup scope and historical database/draft preservation.

Before one separately authorized TEST Deploy: verify exact service/SHA/CI and no
parallel deployment; obtain fresh snapshots and checked restorations of both test
SQLite files; compare all schema/rows and queues; pin the full candidate SHA.
No automatic rollback or old-database replacement. Old code does not fully
understand newer subscription drafts and channel-less activation semantics.

After installation, first inspect without sending. A separate explicit permission
is needed for any live request/notification acceptance: one marked TEST request,
one or two named private test recipients, zero channel publications. Confirm
customer acknowledgement, status, provider in-bot availability/contact/offer and
pause with no duplicate delivery. Production and pilot remain separately gated.
