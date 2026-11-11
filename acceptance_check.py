"""Read-only backend evidence for manual C05/E01; never grants acceptance."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

APP_SHA = 'a5f4eb98877c56fe384b55ff212081af485dee2c'
CLOSE_REASON = 'C05 a5f4eb9 final'
QUESTION = 'E01 a5f4eb9 test'
ANSWER = 'E01 a5f4eb9 ok'


def inspect(database, lead_id, ticket_id):
    database = Path(database)
    if database.is_symlink() or not database.is_file():
        raise ValueError('Existing regular test SQLite required')
    with closing(sqlite3.connect(database.resolve(strict=True).as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        lead = db.execute('SELECT customer_id,status FROM leads WHERE id=?', (lead_id,)).fetchone()
        ticket = db.execute('SELECT requester_id FROM support_tickets WHERE id=?', (ticket_id,)).fetchone()
        events = db.execute("SELECT actor_id,reason FROM lead_lifecycle_events WHERE lead_id=? AND action='closed'",
                            (lead_id,)).fetchall()
        close_audit = db.execute("SELECT actor_id FROM admin_action_log WHERE action='lead_close' AND target_id=? AND result='success'",
                                 (lead_id,)).fetchall()
        messages = db.execute('SELECT id,actor_id,is_admin,text FROM support_messages WHERE ticket_id=?',
                              (ticket_id,)).fetchall()
        questions = [m for m in messages if not m['is_admin'] and m['text'] == QUESTION]
        replies = [m for m in messages if m['is_admin'] and m['text'] == ANSWER]
        reply = replies[0] if len(replies) == 1 else None
        delivery = db.execute('SELECT status,telegram_message_id,error_code FROM support_reply_deliveries WHERE message_id=?',
                               (reply['id'],)).fetchone() if reply else None
        audits = db.execute("SELECT actor_id,action,result,details FROM admin_action_log WHERE target_id=? AND action IN ('support_reply','support_delivery')",
                             (ticket_id,)).fetchall()
        def audit_count(action, result):
            count = 0
            for row in audits:
                try:
                    details = json.loads(row['details'])
                except (TypeError, ValueError):
                    continue
                if (reply and row['actor_id'] == reply['actor_id'] and row['action'] == action
                        and row['result'] == result and isinstance(details, dict)
                        and details.get('message_id') == reply['id']):
                    if action == 'support_delivery' and (not delivery
                            or details.get('telegram_message_id') != delivery['telegram_message_id']
                            or details.get('error_code') is not None):
                        continue
                    count += 1
            return count
        c05 = {
            'record_found': lead is not None,
            'status_closed': bool(lead and lead['status'] == 'closed'),
            'one_closure_with_final_reason': len(events) == 1 and events[0]['reason'] == CLOSE_REASON,
            'one_successful_close_audit': len(close_audit) == 1,
            'closure_actor_matches_audit': bool(len(events) == len(close_audit) == 1
                                               and events[0]['actor_id'] == close_audit[0]['actor_id']),
        }
        e01 = {
            'record_found': ticket is not None,
            'one_question_by_requester': bool(ticket and len(questions) == 1
                                               and questions[0]['actor_id'] == ticket['requester_id']),
            'one_marked_reply': len(replies) == 1,
            'two_distinct_accounts': bool(ticket and reply and ticket['requester_id'] != reply['actor_id']),
            'telegram_delivery_confirmed': bool(delivery and delivery['status'] == 'sent'
                                                 and isinstance(delivery['telegram_message_id'], int)
                                                 and delivery['telegram_message_id'] > 0
                                                 and delivery['error_code'] is None),
            'one_saved_audit': audit_count('support_reply', 'saved') == 1,
            'one_sent_audit': audit_count('support_delivery', 'sent') == 1,
        }
        binding = bool(lead and ticket and reply and len(events) == 1
                       and lead['customer_id'] == ticket['requester_id']
                       and events[0]['actor_id'] == reply['actor_id']
                       and events[0]['actor_id'] != lead['customer_id'])
    return {'lead_id': lead_id, 'ticket_id': ticket_id, 'C05_backend': c05, 'E01_backend': e01,
            'same_two_accounts': binding,
            'backend_checks': 'ok' if all(c05.values()) and all(e01.values()) and binding else 'not_confirmed',
            'C05_cancel_edit_resume_and_notification': 'manual_check_required',
            'E01_human_receipt_and_reopened_history': 'manual_check_required',
            'manual_acceptance': 'pending_owner_confirmation', 'C05': 'open', 'E01': 'open'}


def main():
    if (os.environ.get('RENDER_SERVICE_ID') != 'srv-dboqc51srm7s73cppi00'
            or os.environ.get('RENDER_GIT_COMMIT') != APP_SHA):
        raise SystemExit('STOP: wrong test Worker or installed SHA')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lead-id', required=True, type=int)
    parser.add_argument('--ticket-id', required=True, type=int)
    args = parser.parse_args()
    if min(args.lead_id, args.ticket_id) <= 0:
        parser.error('Positive record IDs required')
    print(json.dumps(inspect('/var/data/bot.db', args.lead_id, args.ticket_id), indent=2))


if __name__ == '__main__':
    main()
