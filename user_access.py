"""Append-only restrictions. No user, profile, lead or draft rows are overwritten."""
import json
import aiosqlite
import admin_access
import storage

SCOPES = frozenset({'requests', 'offers', 'notifications', 'full'})


class AccessConflict(RuntimeError):
    pass


async def init_schema(db_path=None):
    async with aiosqlite.connect(db_path or storage.DB_PATH) as db:
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS user_access_events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                scopes_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                request_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS user_access_latest ON user_access_events(user_id,id);
        ''')
        await db.commit()


def decode(row):
    if row is None:
        return {'revision': 0, 'scopes': [], 'reason': ''}
    scopes = json.loads(row[1])
    if not isinstance(scopes, list) or any(s not in SCOPES for s in scopes):
        raise ValueError('Invalid saved access policy')
    return {'revision': row[0], 'scopes': scopes, 'reason': row[2]}


async def snapshot_in(db, user_id):
    return decode(await (await db.execute(
        'SELECT id,scopes_json,reason FROM user_access_events WHERE user_id=? ORDER BY id DESC LIMIT 1',
        (user_id,),
    )).fetchone())


async def snapshot(user_id, db_path=None):
    async with aiosqlite.connect(db_path or storage.DB_PATH) as db:
        return await snapshot_in(db, user_id)


async def allowed(user_id, scope, db_path=None):
    if scope not in SCOPES:
        return False
    saved = await snapshot(user_id, db_path)
    return 'full' not in saved['scopes'] and scope not in saved['scopes']


async def change(actor_id, user_id, scopes, reason, request_id, *, expected_revision, owner_id, legacy_admins=()):
    if owner_id is None or actor_id != owner_id:
        raise PermissionError('Только владелец может менять ограничения')
    if type(user_id) is not int or not 0 < user_id < 2**63 or user_id == owner_id:
        raise ValueError('Нельзя ограничить владельца')
    if not isinstance(scopes, (list, tuple, set)) or any(s not in SCOPES for s in scopes):
        raise ValueError('Неизвестное ограничение')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise ValueError('Укажите причину до 500 символов')
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64 or type(expected_revision) is not int:
        raise ValueError('Некорректное подтверждение')
    encoded, reason = json.dumps(sorted(set(scopes))), reason.strip()
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute('BEGIN IMMEDIATE')
        previous = await (await db.execute(
            'SELECT actor_id,user_id,scopes_json,reason FROM user_access_events WHERE request_id=?', (request_id,),
        )).fetchone()
        if previous:
            if previous != (actor_id, user_id, encoded, reason):
                raise ValueError('Повторное подтверждение содержит другие данные')
            return False
        role = await admin_access.role_snapshot_in(db, user_id, legacy_admins, owner_id)
        if role['role'] != 'none':
            raise ValueError('Сначала отзовите административные права этого пользователя')
        old = await snapshot_in(db, user_id)
        if old['revision'] != expected_revision:
            await db.execute('INSERT INTO admin_action_log(actor_id,action,target_id,result,details,created_at) VALUES (?,?,?,?,?,?)',
                             (actor_id,'user_access',user_id,'conflict',json.dumps({'expected_revision':expected_revision,
                              'actual_revision':old['revision']}),admin_access.now()))
            await db.commit()
            raise AccessConflict('Ограничения изменились; проверьте заново')
        stamp = admin_access.now()
        await db.execute('INSERT INTO user_access_events(user_id,scopes_json,reason,actor_id,request_id,created_at) VALUES (?,?,?,?,?,?)',
                         (user_id,encoded,reason,actor_id,request_id,stamp))
        await db.execute('INSERT INTO admin_action_log(actor_id,action,target_id,result,details,created_at) VALUES (?,?,?,?,?,?)',
                         (actor_id,'user_access',user_id,'success',json.dumps({'previous':old['scopes'],
                          'scopes':json.loads(encoded),'reason':reason},ensure_ascii=False),stamp))
        await db.commit()
        return True


async def filter_users(user_ids, scope, db_path=None):
    if scope not in SCOPES:
        raise ValueError('Unknown scope')
    ids = list(user_ids)
    excluded = set()
    async with aiosqlite.connect(db_path or storage.DB_PATH) as db:
        await db.execute('BEGIN')
        for offset in range(0,len(ids),800):
            batch = ids[offset:offset+800]
            placeholders = ','.join('?' for _ in batch)
            rows = await (await db.execute(
                'SELECT user_id,scopes_json FROM user_access_events WHERE id IN '
                '(SELECT max(id) FROM user_access_events WHERE user_id IN ('+placeholders+') GROUP BY user_id)',batch,
            )).fetchall()
            for user_id, encoded in rows:
                saved = decode((0,encoded,''))['scopes']
                if 'full' in saved or scope in saved:
                    excluded.add(user_id)
    return [i for i in ids if i not in excluded]
