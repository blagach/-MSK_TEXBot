# TEST acceptance follow-up, 16 November 2026

Prepared on exact installed TEST `967b1e40f12f3cf28774872bc6cbdf542ac8a889`,
branch `fix/test-screen-controls-offer`, stacked on PR24. No release, service
configuration change, live SQLite write, real Telegram request or pilot is part
of this change. The preceding live acceptance (lead 28, offer 9, support 8)
must not be repeated wholesale.

## G05: evidence and limits

Live logs showed repeated `TelegramBadRequest` in both form keyboard cleanup
and shared screen retirement. They stored only the exception class. The exact
Telegram description for those historical requests is therefore **unavailable**;
we cannot prove every historical warning had the same cause or that all were
harmless.

A concrete defect is reproduced against the original installed code with a
strict fake Telegram backend: the form handler successfully removes its panel,
then `ScreenControls.retire` removes the same keyboard again in the same update.
The fake backend returns Telegram's `message is not modified` on the second
identical edit; the original controller emits the observed generic warning.
The regression fails on the original controller and passes on the candidate.
This proves a duplicate-cleanup cause, not the unpublished contents of past
Telegram error responses.

Successful cleanup is now remembered only for the current actor/chat/task/update
and exact target markup. The controller skips that identical retirement. Adding
new controls clears the memoized result. Returning to a reply keyboard retires
the current temporary panels, while durable contacts and history links remain.
Persisted panel dictionaries are copied to avoid in-memory aliasing during a
same-update transition. No schema change, message deletion, retries or new
notification is introduced.

Only `message is not modified` is classified as already applied (debug).
Missing/uneditable messages, invalid IDs, unexpected bad requests and other API
failures remain warnings. Logs identify the cleanup operation and a safe error
category; arbitrary Telegram payloads, URLs, phone numbers and credentials are
not logged. Invalidation stays durable **before** visual edits. A genuine failed
edit can leave old buttons visible, but the existing stale-screen guard still
rejects their actions after navigation and SQLite reopen. Such failures must be
investigated rather than called fixed by this classification.

## Offer button and approved interface

`tz7-decisions-2026-11-11.md` and `tz7-stage1-spec.md` retain the old offer flow
for compatibility while making contact access the primary path. The previous
`private_contact_card` and notification markup contained only the contact
button. Thus the missing button was consistent with the simplified rendering,
not loss of the offer FSM itself. The owner's 16 November follow-up calls for
an accessible button using that retained flow.

The candidate adds a short `Откликнуться` button on a separate full-width row
below the existing contact button in matching cards and private notifications.
Contact reveal preserves the new row. No extra menu, new offer table, independent
wizard, payment or channel publication is added. New notification fanout is not
introduced; only its markup changes.

Historical callers may pass only a lead ID to contact rendering or a minimal
delivery seed to notification fanout. Those input contracts are preserved:
offer availability is read from SQLite, and notification markup uses the
current database lead rather than adding required fields to the seed.

`offer_open:<id>` verifies the private actor/chat, active foreign lead, registered
unblocked provider and current offer restrictions, then calls the existing
`open_offer` implementation. Existing price, conditions, phone, review, draft
recovery, cancellation, transactional duplicate guard and delivery logic remain.
Old durable cards recheck current status and rights; closed cards are denied,
restored active cards can reopen an unsent draft, and submitted offers cannot
be sent again. Ownership restrictions apply to the new button; legacy command
behavior and storage semantics were not rewritten.

The one-column layout and labels are checked from actual aiogram markup.
Actual smartphone rendering of this candidate cannot be claimed without its
separately authorized TEST installation.

## Validation

79 addressed tests passed with socket connections disabled and **zero network
attempts**: screen controls, card offer entry, moderation/restoration, in-bot
publication, notification delivery, legacy contact rendering, two-account
delivery isolation and the existing offer UI. Ten new tests
cover the reproduced double clear, error classification, cache invalidation,
button/contact markup, the full existing offer FSM, duplicate and concurrent
confirmation, blocked/wrong actors, closed/restored leads and unchanged SQLite
schema/business rows. Synthetic SQLite only; actual TEST databases were not
opened for writing or migrated.

The original-controller reproduction failed its no-warning assertion exactly
once as expected; it is a baseline reproduction, not a candidate failure.
Tests deliberately emit warnings for real simulated edit failures and Telegram
429/unknown outcomes; those warnings must remain visible.

The repository's existing CI runs its full suite automatically on the PR. Its
result must be checked against the exact final candidate SHA before release.
This is not a second local full acceptance or a replacement for targeted tests.

## Registry and next gate

Preserve the accepted register: 65 ready / 2 requiring verification. G05 has an
offline-tested candidate but remains open until a small TEST check confirms
screen cleanup on the installed candidate and categorizes any remaining genuine
Telegram failures. G08 remains with the owner and is not closed automatically.
No previously accepted requirement is reclassified by the stale repository
table's older statuses.

After separate permission for one exact-SHA TEST Deploy, perform the existing
fresh-backup/restore and queue preflight. Then check only the changed cleanup and
offer-entry behavior with approved actors. No automatic rollback: restored
leads remain incompatible with old one-close assumptions. Never overwrite the
live SQLite with a historical snapshot. Production and pilot remain prohibited.
