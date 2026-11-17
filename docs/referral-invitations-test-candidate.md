# Referral invitations — isolated TEST candidate

Base: `242e23ae31a87af5c4da57e008a94518295b0337`. No deployed environment, BotFather configuration, live SQLite, Telegram recipient, channel, or backup is changed by this work.

## Delivery contract

The common private main-menu message gains a one-column **Поделиться ботом** inline entry only while referrals are enabled. Existing reply keyboards and roles remain. The section contains **Отправить приглашение**, **Копировать ссылку**, **Моя статистика**, **Назад**, and **Главное меню**. Only aggregate numbers are shown. Existing ScreenControls handles stale callback panels; persistent invitation URLs cannot be revoked after a user has copied them.

`getMe.supports_inline_queries` selects the primary button without enabling anything. If true, `switch_inline_query_chosen_chat` allows private users/groups and excludes bots/channels. The user selects a chat, then **selects the inline result to send it immediately**. Telegram supplies no additional confirmation dialog after selection. Neither chosen-result feedback nor recipient contact access is needed or requested. If false, the primary button directly opens `t.me/share/url` with an encoded invitation and personal link. The same fallback remains visible when inline is available.

Only the exact `invite` inline query is accepted. The result is generated from the sender's Telegram identity, never a code supplied in the query, and uses `is_personal=True, cache_time=0`. Channel queries get no result. Answers provide a result to Telegram, not a bot `sendMessage` to a chosen recipient. Forwarding or selecting the result does not increment referrals. A user may still forward copied text manually; the application never enumerates contacts or sends invitations automatically.

Official references: [chat selection](https://core.telegram.org/bots/api#switchinlinequerychosenchat), [inline mode and sending](https://core.telegram.org/bots/inline), [fallback share link](https://core.telegram.org/widgets/share).

## Persistence and accounting

Two additive tables (`referral_links`, `referral_attributions`) and one index; no alterations or backfill of historical business rows. Link codes are stable random 128-bit URL-safe strings, without exposed Telegram IDs. Links use the configured bot username, so TEST and Production remain separate.

Default is **OFF**, stored as the absence of an enabled `referral_mode` event in the existing admin audit. Statistics -> Invitations exposes aggregate counts; only the owner can enable/pause with a revision-checked confirmation. No Render variable is added. Pause stops new attribution/events and hides the entry; old referral starts open the normal menu. Existing metrics remain. No automatic replay/backfill for activity during pause.

First-touch eligibility and visitor insertion share a short SQLite write transaction BEFORE ScreenControls creates its own draft. The normal visitor middleware originally ran after ScreenControls; moving it before controls is required to avoid treating every new actor as historical. Repeated updates, competing links, self-invitations, known profiles/visitors/leads/offers/support/legacy drafts/link owners and blocked accounts are excluded. Organic, malformed, or self-referral first visits are not eligible for later reassignment. Existing `lead_`/`contact_` start routes remain intact.

Count an active invited account once if either first successful in-bot lead activation or completed modern provider registration is recorded. A profile alone is incomplete until nonempty payment preferences are saved. Equipment inventory is optional. Milestone writes share the existing business transaction, survive restart, and are not reset on closure/restoration or profile edits. Legacy provider editing does not create a modern registration event. Counts do not identify unique natural persons across multiple Telegram accounts.

## Verification and limits

`tests/test_referral_invitations.py` exercises default-off, atomic first-touch before UI middleware, duplicate/competing starts, historical and blocked accounts, organic/malformed/self starts, reopen/idempotence, two milestones with one active account, partial registration, payment/metric rollback on injected SQLite error, pause/stale revisions/nonowner, aggregate privacy, inline sender binding, personal cache settings, fallback URL decoding, copy link, single-column layouts, actual dispatcher inline transport, foreign private cards, channel requests and additive migration/restore against a reconstructed historical schema. All storage is temporary and all Telegram transport is stubbed.

Run the addressed tests with `python -m unittest discover -s tests -p test_referral_invitations.py -v`. CI runs the existing full suite, including the old lead/contact/offer and moderation paths. Local regression additionally blocks socket connections and live SQLite paths; record final counts in the review report.

No installed TEST client can render this uninstalled candidate. iOS, Android and Desktop visual behavior remains **unverified**, including chat selection, the inline result's immediate-send behavior, client fallback and old-client copy support. Do not present the keyboard structure test as a smartphone acceptance test.

## Controlled next stage (separate authorization)

1. Review the exact candidate, CI and additive-schema evidence; retain the existing acceptance baseline.
2. Read the actual TEST SHA, worker, queues and schema. Back up both TEST SQLite files and independently restore/compare copies without replacing either live DB.
3. If separately authorized, perform one TEST **Deploy a specific commit** with the exact approved SHA. Do not update Render environment variables. Keep referrals OFF initially.
4. Verify the installed SHA, one worker, intact existing data and queues. Two new tables are expected in the core database, not a copy of the Production database.
5. Separately authorize enabling referrals in TEST and any live client acceptance. Inline mode via BotFather `/setinline` is a distinct external configuration change requiring authorization; do not enable location or chosen-result feedback. Fallback works without inline mode.
6. Use only approved TEST recipients. Existing actors validate interface/old-user exclusion; a previously unused approved TEST actor is needed for new-user attribution. Do not erase history to simulate one.
7. On iOS/Android/Desktop check chat chooser and cancellation before selection; sending an actual inline result or fallback invitation requires explicit authorization. No Production changes or pilot.

Residual risks: a referral SQL failure can roll back the business transaction, intentionally preventing partial accounting; validate schema and use the owner pause to disable event writes. Shared SQLite still requires one worker. A copied link remains shareable while paused but creates no attribution. Client-specific rendering and BotFather capability await live TEST approval. No automatic rollback, historical resend, or live database restore is permitted.
