"""Persistent TEST administrative screens, always using fake campaign delivery.

Rendered locally by LocalPreviewSession, or by the explicitly verified isolated
test adapter. No real payment provider or campaign sender is accepted.
"""
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sandbox_campaigns import FakeTransport, SandboxCampaigns, moscow_instant
from sandbox_payments import FakePaymentProvider, SandboxPayments, canonical, positive
from sandbox_store import instant
from sandbox_scheduler import SandboxScheduler


@dataclass(frozen=True)
class Panel:
    text: str
    buttons: tuple = ()  # (visible label, opaque callback)
    expects_input: bool = False
    preview_text: str = ''
    attachments: tuple = ()


class LocalAdmin:
    def __init__(self, store, owner, marketers=(), *, transport=None):
        self.store, self.owner = store, positive(owner)
        self.marketers = set(marketers)
        self.payments = SandboxPayments(store)
        self.campaigns = SandboxCampaigns(store, owner, marketers)
        self.transport = transport if transport is not None else FakeTransport()
        if type(self.transport) is not FakeTransport:
            raise TypeError('Only FakeTransport is permitted')
        self.scheduler = SandboxScheduler(self.campaigns, self.transport)
        self.provider = FakePaymentProvider()
        self.lock = threading.RLock()
        store.schema('''
            CREATE TABLE IF NOT EXISTS sandbox_admin_sessions (
                actor INTEGER PRIMARY KEY, screen TEXT NOT NULL, data TEXT NOT NULL,
                nonce TEXT NOT NULL, busy INTEGER NOT NULL DEFAULT 0, prompt_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS sandbox_admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor INTEGER NOT NULL,
                action TEXT NOT NULL, object_id TEXT NOT NULL, at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sandbox_consent_panels (
                recipient INTEGER PRIMARY KEY REFERENCES sandbox_recipients(id),
                nonce TEXT NOT NULL, panel_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS sandbox_admin_prompts (
                actor INTEGER NOT NULL, message_id INTEGER NOT NULL, nonce TEXT NOT NULL,
                PRIMARY KEY(actor,message_id)
            );
        ''')

    def allowed(self, actor, *, finance=False):
        positive(actor)
        if actor != self.owner and (finance or actor not in self.marketers):
            raise PermissionError('Нет доступа к этому разделу.')
        # Refresh current local permissions on every interaction, not on start.
        self.campaigns.editors = frozenset(self.marketers | {self.owner})

    def consent_panel(self, actor):
        positive(actor)
        with self.store.transaction() as db:
            row = db.execute('SELECT consent FROM sandbox_recipients WHERE id=?', (actor,)).fetchone()
            if not row:
                raise ValueError('Получатель не зарегистрирован в локальном контуре.')
            nonce = uuid4().hex[:12]
            db.execute('''INSERT INTO sandbox_consent_panels(recipient,nonce) VALUES (?,?)
                ON CONFLICT(recipient) DO UPDATE SET nonce=excluded.nonce''', (actor, nonce))
        return Panel('Административные сообщения: '+('включены.' if row[0] else 'выключены.')+
                     ' Это отдельное согласие, не уведомления о заявках.',
                     (('Согласен получать сообщения', f'sc:{nonce}:on'), ('Отказаться от сообщений', f'sc:{nonce}:off')))

    def consent_response(self, actor, nonce, allowed, now):
        positive(actor)
        with self.store.transaction() as db:
            if not db.execute('UPDATE sandbox_consent_panels SET nonce=? WHERE recipient=? AND nonce=?',
                              (uuid4().hex[:12], actor, nonce)).rowcount:
                raise ValueError('Это чужая или устаревшая кнопка согласия.')
            self.campaigns.record_consent(db, actor, allowed, now)

    def session(self, actor):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM sandbox_admin_sessions WHERE actor=?', (actor,)).fetchone()
            return dict(row) if row else None

    def save(self, actor, screen, data):
        with self.store.transaction() as db:
            db.execute('''INSERT INTO sandbox_admin_sessions VALUES (?,?,?,?,0,NULL)
                ON CONFLICT(actor) DO UPDATE SET screen=excluded.screen,data=excluded.data,
                nonce=excluded.nonce,busy=0,prompt_id=NULL''',
                       (actor, screen, canonical(data), uuid4().hex[:12]))

    def start(self, actor):
        with self.lock:
            self.allowed(actor)
            with self.store.transaction() as db:
                s = db.execute('SELECT * FROM sandbox_admin_sessions WHERE actor=?', (actor,)).fetchone()
                if not s:
                    db.execute('INSERT INTO sandbox_admin_sessions VALUES (?,\'home\',\'{}\',?,0,NULL)',
                               (actor, uuid4().hex[:12]))
                elif not s['busy']:
                    db.execute('UPDATE sandbox_admin_sessions SET nonce=?,prompt_id=NULL WHERE actor=?',
                               (uuid4().hex[:12], actor))
            return self.render(actor)

    def set_prompt(self, actor, nonce, message_id):
        with self.store.transaction() as db:
            db.execute('UPDATE sandbox_admin_sessions SET prompt_id=? WHERE actor=? AND nonce=? AND busy=0',
                       (message_id, actor, nonce))
            db.execute('INSERT OR IGNORE INTO sandbox_admin_prompts VALUES (?,?,?)', (actor, message_id, nonce))

    def render(self, actor):
        self.allowed(actor)
        s = self.session(actor)
        if not s:
            raise ValueError('Сначала откройте локальное меню.')
        state, d = s['screen'], json.loads(s['data'])
        def button(label, action, value=''):
            callback = ':'.join(('sb', s['nonce'], action, value))
            assert len(callback.encode()) <= 64
            return label, callback
        home = button('Главное меню', 'home')
        if s['busy']:
            return Panel('Операция прервана. Автоматический повтор запрещён. '
                         'Проверьте карточку и журнал перед новым действием.', (home,))
        if state == 'home':
            rows = [button('Кампании', 'campaigns'), button('Согласия: сводка', 'consents')]
            if actor == self.owner:
                rows.insert(0, button('Платежи — TEST', 'payments'))
            return Panel('Локальный тестовый контур. Реальные деньги и сообщения отключены.', tuple(rows))
        if state == 'consents':
            with self.store.connection() as db:
                counts = dict(db.execute('SELECT consent,count(*) FROM sandbox_recipients GROUP BY consent'))
            return Panel(f"Согласились: {counts.get(1, 0)}. Без согласия/отказались: {counts.get(0, 0)}. "
                         'Администратор не может дать согласие за получателя.', (home,))
        if state in ('payments', 'campaigns'):
            finance = state == 'payments'
            self.allowed(actor, finance=finance)
            page = d.get('page', 0)
            table = 'sandbox_orders' if finance else 'sandbox_campaigns'
            with self.store.connection() as db:
                rows = db.execute('SELECT * FROM '+table+' ORDER BY id LIMIT 9 OFFSET ?', (page*8,)).fetchall()
            buttons = []
            if not finance:
                buttons.append(button('Создать кампанию', 'new'))
            for row in rows[:8]:
                status = self.payments.get_order(row['payer'], row['id'])['status'] if finance else row['state']
                buttons.append(button(f"{row['id'][:8]} · {status}", 'pay' if finance else 'camp', row['id']))
            if page:
                buttons.append(button('Предыдущая страница', 'page', str(page-1)))
            if len(rows) > 8:
                buttons.append(button('Следующая страница', 'page', str(page+1)))
            buttons.append(home)
            return Panel('TEST-платежи' if finance else 'Кампании — подставной транспорт', tuple(buttons))
        if state == 'pay':
            self.allowed(actor, finance=True)
            with self.store.connection() as db:
                row = db.execute('SELECT * FROM sandbox_orders WHERE id=?', (d['id'],)).fetchone()
                if not row:
                    raise ValueError('Заказ не найден.')
                order = self.payments._order_view(db, row)
                right = db.execute('SELECT remaining,expires_at FROM sandbox_rights WHERE order_id=?', (d['id'],)).fetchone()
                events = db.execute('SELECT status FROM sandbox_payment_outcomes WHERE order_id=? ORDER BY rowid DESC LIMIT 8',
                                    (d['id'],)).fetchall()
            status = order['status']
            actions = ('paid', 'failed', 'cancelled') if status in ('pending', 'failed') else ('refunded',) if status == 'paid' else ()
            labels = {'paid': 'Тест: подтвердить оплату', 'failed': 'Тест: ошибка оплаты',
                      'cancelled': 'Тест: отменить заказ', 'refunded': 'Тест: полный возврат'}
            buttons = [button(labels[x], 'pask', x) for x in actions]
            buttons.extend((button('К списку платежей', 'payments'), home))
            expiry = order.get('grant_expires_at', order['expires_at'])
            expires = (datetime.fromtimestamp(expiry, ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК')
                       if expiry is not None else 'после подтверждения' if order.get('duration') else 'без ограничения')
            quota = ('Доступ на срок, без расхода квоты' if order.get('grant_mode') == 'period' else
                     f"Квота: {order['units']}; остаток: {right['remaining'] if right else 0}")
            text = (f"Заказ {order['id']}\nСтатус: {status}\nПлательщик: {order['payer']}\n"
                    f"Условная сумма: {order['amount']} TEST — не тариф\n{quota}\nСрок права: {expires}\n"
                    f"Последние события: {', '.join(r[0] for r in events) or 'нет'}")
            return Panel(text, tuple(buttons))
        if state == 'pconfirm':
            self.allowed(actor, finance=True)
            return Panel(f"Подтвердить только подставное событие {d['outcome']} для TEST-заказа {d['id']}? "
                         'Настоящих списаний нет.',
                         (button('Подтвердить тестовое действие', 'papply'), button('Назад к заказу', 'pay', d['id']), home))
        if state == 'role':
            return Panel('Выберите аудиторию кампании.',
                         (button('Заказчики', 'role', 'customer'), button('Исполнители', 'role', 'provider'),
                          button('Обе группы', 'role', 'both'), home))
        if state == 'region':
            return Panel('Выберите территорию.', (button('Москва', 'region', 'msk'),
                         button('Московская область', 'region', 'mo'), button('Москва и область', 'region', 'both'), home))
        if state == 'categories':
            return Panel('Выберите виды техники для сегмента.',
                         (button('Все виды техники', 'allcats'), button('Указать виды техники', 'cats'), home))
        prompts = {'cats': 'Ответьте на это сообщение: виды техники через запятую.',
                   'compose': 'Ответьте текстом кампании или одним фото, видео, документом с подписью.',
                   'edit': 'Ответьте новым текстом. Существующие вложения будут сохранены.',
                   'attach': 'Ответьте одним фото, видео или документом. Оно добавится в новую редакцию.',
                   'schedule': 'Ответьте датой и временем по Москве: ГГГГ-ММ-ДД ЧЧ:ММ.'}
        if state in prompts:
            return Panel(prompts[state], (home,), expects_input=True)
        if state == 'camp':
            with self.store.connection() as db:
                row = db.execute('SELECT * FROM sandbox_campaigns WHERE id=?', (d['id'],)).fetchone()
            if not row:
                raise ValueError('Кампания не найдена.')
            report = self.campaigns.report(actor, d['id'])
            buttons = []
            if any(r['status'] == 'uncertain' for r in report):
                buttons.append(button('Неопределённые результаты', 'quarantine'))
            if row['state'] in ('draft', 'approved'):
                rev = str(row['revision'])
                buttons.extend((button('Изменить текст', 'edit', rev), button('Добавить вложение', 'attach', rev),
                                button('Редакция без вложений', 'nomedia', rev), button('Предпросмотр и расписание', 'schedule', rev)))
            if actor == self.owner and row['state'] in ('approved', 'running'):
                buttons.append(button('Пауза', 'pause', str(row['revision'])))
            if actor == self.owner and row['state'] == 'paused':
                buttons.append(button('Продолжить', 'resume', str(row['revision'])))
            if actor == self.owner and row['state'] in ('approved', 'running'):
                buttons.append(button('Один шаг подставной очереди', 'step', str(row['revision'])))
            buttons.extend((button('Обновить отчёт', 'camp', d['id']), button('К кампаниям', 'campaigns'), home))
            summary = '; '.join(f"r{r['revision']} {r['status']}: {r['recipients']}" for r in report) or 'Очередь ещё не создана.'
            with self.store.connection() as db:
                events = db.execute('SELECT outcome FROM sandbox_campaign_events WHERE campaign=? ORDER BY id DESC LIMIT 8',
                                    (d['id'],)).fetchall()
            run_at = (datetime.fromtimestamp(row['run_at'], ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M:%S МСК')
                      if row['run_at'] is not None else 'не задано')
            return Panel(f"Кампания {d['id']}\nРедакция: {row['revision']}; статус: {row['state']}\nВремя: {run_at}\n"
                         f"{summary}\nЖурнал: {', '.join(r[0] for r in events) or 'нет событий'}\n"
                         'sent означает ответ подставного транспорта, а не прочтение.', tuple(buttons))
        if state == 'quarantine':
            with self.store.connection() as db:
                rows = db.execute('''SELECT q.revision,q.recipient,coalesce(x.reason,'historical_uncertain') AS reason
                    FROM sandbox_campaign_queue q LEFT JOIN sandbox_campaign_quarantine x
                    ON x.campaign=q.campaign AND x.revision=q.revision AND x.recipient=q.recipient
                    WHERE q.campaign=? AND q.status='uncertain' ORDER BY q.revision,q.recipient LIMIT 10''', (d['id'],)).fetchall()
            text = '\n'.join(f"r{r['revision']} · {r['recipient']} · {r['reason']}" for r in rows)
            return Panel('Неопределённые результаты. Автоповтора нет.\n'+(text or 'Нет записей.'),
                         (button('К кампании', 'camp', d['id']), home))
        if state == 'preview':
            p = d['preview']
            at = datetime.fromtimestamp(p['run_at'], ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M:%S МСК')
            buttons = [button('Подтвердить кампанию', 'approve')] if actor == self.owner else []
            buttons.extend((button('К кампании', 'camp', p['campaign']), home))
            return Panel(f"Предпросмотр r{p['revision']}. Адресатов: {len(p['recipients'])}.\n"
                         f"Отправка: {at}\nТолько подставной транспорт. Подтверждает владелец.", tuple(buttons),
                         preview_text=p['payload']['text'], attachments=tuple(p['payload']['attachments']))
        raise ValueError('Неизвестный экран; операция не будет повторена.')

    def handle(self, actor, callback, now):
        instant(now)
        with self.lock:
            self.allowed(actor)
            s = self.session(actor)
            if not s or callback not in {b[1] for b in self.render(actor).buttons}:
                raise ValueError('Кнопка устарела или действие не разрешено.')
            _, nonce, action, value = callback.split(':', 3)
            self._claim(actor, nonce, action, now)
            d, screen = json.loads(s['data']), s['screen']
            try:
                if action in ('home', 'payments', 'campaigns', 'consents'):
                    if action == 'payments':
                        self.allowed(actor, finance=True)
                    screen, d = action, {}
                elif action == 'page':
                    d['page'] = int(value)
                elif action in ('pay', 'camp'):
                    screen, d = action, {'id': value}
                elif action == 'pask':
                    self.allowed(actor, finance=True)
                    screen, d = 'pconfirm', {**d, 'outcome': value}
                elif action == 'papply':
                    self.allowed(actor, finance=True)
                    with self.store.connection() as db:
                        order = dict(db.execute('SELECT * FROM sandbox_orders WHERE id=?', (d['id'],)).fetchone())
                    event = self.provider.event(order, d['outcome'], f'ui:{actor}:{nonce}')
                    self.payments.apply_event(self.provider, event, now=now)
                    screen, d = 'pay', {'id': d['id']}
                elif action == 'new':
                    screen, d = 'role', {}
                elif action == 'role':
                    screen, d = 'region', {'roles': ['customer', 'provider'] if value == 'both' else [value]}
                elif action == 'region':
                    d['zones'] = {'msk': ['Москва'], 'mo': ['Московская область'],
                                  'both': ['Москва', 'Московская область']}[value]
                    screen = 'categories'
                elif action == 'allcats':
                    screen, d = 'compose', {**d, 'categories': []}
                elif action == 'cats':
                    screen = 'cats'
                elif action == 'quarantine':
                    screen = 'quarantine'
                elif action in ('edit', 'attach', 'schedule', 'nomedia', 'pause', 'resume', 'step'):
                    with self.store.connection() as db:
                        row = dict(db.execute('SELECT * FROM sandbox_campaigns WHERE id=?', (d['id'],)).fetchone())
                        version = db.execute('SELECT payload FROM sandbox_campaign_revisions WHERE campaign=? AND revision=?',
                                             (d['id'], row['revision'])).fetchone()
                    d['revision'] = row['revision']
                    if str(row['revision']) != value:
                        raise ValueError('Редакция изменилась; откройте новую карточку.')
                    if action in ('edit', 'attach', 'schedule'):
                        screen = action
                    elif action == 'nomedia':
                        self.campaigns.edit(actor, d['id'], d['revision'], json.loads(version[0])['text'], [])
                    elif action in ('pause', 'resume'):
                        getattr(self.campaigns, action)(actor, d['id'], d['revision'], now)
                    elif action == 'step':
                        self.allowed(actor, finance=True)
                        if self.scheduler.tick(now, campaign=d['id'])['busy']:
                            raise ValueError('Очередь уже обрабатывается; обновите отчёт.')
                elif action == 'approve':
                    p = d['preview']
                    if p['run_at'] < now:
                        raise ValueError('Время предпросмотра прошло; задайте новое расписание.')
                    self.campaigns.approve(actor, p['campaign'], p['revision'], p['approval_token'], p['run_at'])
                    screen, d = 'camp', {'id': p['campaign']}
                else:
                    raise ValueError('Неизвестное действие.')
                self.save(actor, screen, d)
                return self.render(actor)
            except (ValueError, PermissionError):
                self.save(actor, s['screen'], json.loads(s['data']))
                raise
            # Other exceptions deliberately leave a durable busy marker. There
            # is no automatic replay after an uncertain local operation.

    def _claim(self, actor, nonce, action, now):
        with self.store.transaction() as db:
            changed = db.execute('UPDATE sandbox_admin_sessions SET busy=1 WHERE actor=? AND nonce=? AND busy=0',
                                 (actor, nonce)).rowcount
            # Busy screen permits only an explicit exit, never repeating I/O.
            if not changed and action == 'home':
                changed = db.execute('UPDATE sandbox_admin_sessions SET busy=1 WHERE actor=? AND nonce=?',
                                     (actor, nonce)).rowcount
            if not changed:
                raise ValueError('Действие уже выполняется или экран устарел.')
            db.execute('INSERT INTO sandbox_admin_audit(actor,action,object_id,at) VALUES (?,?,?,?)',
                       (actor, action, nonce, now))

    def text(self, actor, nonce, text, now, attachment=None):
        instant(now)
        with self.lock:
            self.allowed(actor)
            s = self.session(actor)
            if not s or s['nonce'] != nonce or not self.render(actor).expects_input:
                raise ValueError('Ответ относится к старому экрану.')
            self._claim(actor, nonce, 'input', now)
            d, screen = json.loads(s['data']), s['screen']
            try:
                if screen == 'cats':
                    values = sorted(set(v.strip() for v in text.split(',') if v.strip()))
                    if not values or len(values) > 20 or any(len(v) > 80 for v in values):
                        raise ValueError('Укажите от 1 до 20 читаемых видов техники.')
                    d['categories'], screen = values, 'compose'
                elif screen == 'compose':
                    cid = self.campaigns.draft(actor, text, d['roles'], d['zones'], d['categories'],
                                               [attachment] if attachment else [])
                    screen, d = 'camp', {'id': cid}
                elif screen in ('edit', 'attach'):
                    with self.store.connection() as db:
                        row = db.execute('SELECT payload FROM sandbox_campaign_revisions WHERE campaign=? AND revision=?',
                                         (d['id'], d['revision'])).fetchone()
                    p = json.loads(row[0])
                    if screen == 'attach' and attachment is None:
                        raise ValueError('Нужно фото, видео или документ.')
                    self.campaigns.edit(actor, d['id'], d['revision'], text if screen == 'edit' else p['text'],
                                        p['attachments'] if screen == 'edit' else p['attachments']+[attachment])
                    screen, d = 'camp', {'id': d['id']}
                elif screen == 'schedule':
                    at = moscow_instant(text)
                    if at < now:
                        raise ValueError('Укажите время не раньше текущего.')
                    p = self.campaigns.preview(actor, d['id'], at)
                    screen, d = 'preview', {'preview': p}
                else:
                    raise ValueError('Здесь ввод не ожидается.')
                self.save(actor, screen, d)
                return self.render(actor)
            except (ValueError, PermissionError):
                self.save(actor, s['screen'], json.loads(s['data']))
                raise
