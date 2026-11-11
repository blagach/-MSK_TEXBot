# Проверка копии с телефона или ноутбука

Сейчас фактическая копия Render **не подтверждена**. Ниже — инструкция владельцу, а не отчёт о выполненной проверке. Обновление не запускается.

1. Откройте Render своим обычным способом и выберите **My Workspace → msk-texbot-test → Shell**. Рабочий сервис не открывайте. Если Shell неудобен на телефоне, используйте ноутбук.
2. Скопируйте весь блок ниже и вставьте в Shell один раз. Нажмите Enter, если терминал ожидает подтверждения ввода. Команда сама проверит ID сервиса, создаст снимок и восстановит его **в новый файл**. Ручной ввод путей и хеша не нужен.
3. Дождитесь JSON-отчёта с `integrity: ok`, `foreign_keys: ok`, `schema: ok` и числом проверенных таблиц. При любой ошибке остановитесь; не удаляйте файлы и не восстанавливайте их поверх действующей базы.
4. Пришлите этот отчёт текстом или скриншотом. Он содержит только пути, контрольные суммы, количества и наличие контрольных ID — без телефонов, паролей, токенов и переписки.

```bash
python - <<'PY'
import os, json, hashlib
from contextlib import closing
from render_test_start import TEST_WORKER_ID, test_database_path
from render_test_backup import snapshot, restore_candidate, verify_preserved_rows, open_read_only

def digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()

if os.environ.get('RENDER_SERVICE_ID') != TEST_WORKER_ID:
    raise SystemExit('STOP: это не разрешённый тестовый Worker')
source = test_database_path()
if not source.is_file():
    raise SystemExit('STOP: тестовая SQLite не найдена')
copy, checksum = snapshot(source)
if digest(copy) != checksum:
    raise SystemExit('STOP: SHA-256 снимка не совпал')
restored = restore_candidate(copy, checksum)
tables = verify_preserved_rows(copy, restored)
with closing(open_read_only(copy)) as before, closing(open_read_only(restored)) as after:
    query = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    if before.execute(query).fetchall() != after.execute(query).fetchall():
        raise SystemExit('STOP: схема восстановленного файла отличается')
if digest(copy) != checksum:
    raise SystemExit('STOP: снимок изменился во время проверки')
with closing(open_read_only(copy)) as db:
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {name: db.execute('SELECT count(*) FROM "'+name+'"').fetchone()[0]
              for name in ('leads','offers','providers','provider_profiles','provider_equipment',
                           'form_drafts','support_tickets','support_messages','support_status_events')
              if name in names}
    control_ids = {str(i): bool(db.execute('SELECT 1 FROM leads WHERE id=?',(i,)).fetchone())
                   for i in (8,9,11,14)}
print(json.dumps({'worker_id': TEST_WORKER_ID, 'snapshot': str(copy), 'snapshot_sha256': checksum,
                  'restored': str(restored), 'restored_sha256': digest(restored),
                  'source_tables_verified': tables, 'integrity': 'ok', 'foreign_keys': 'ok', 'schema': 'ok',
                  'counts': counts, 'lead_ids_present': control_ids}, ensure_ascii=False, indent=2))
PY
```

Снимок создаётся штатным SQLite backup API. Исходная база открывается только для чтения; схема не мигрирует, файлы не заменяются и не удаляются. Целостность, внешние ключи и полное сравнение исходных строк выполняют функции существующего `render_test_backup.py`. Блок использует уже существующие модули тестового запуска; установка нового кода или deploy для этой команды не нужны.

SHA-256 снимка проверяется независимо дважды. Хеш восстановленного файла указан отдельно: полное совпадение строк и схемы проверяется отдельными запросами, а не предполагается только из совпадения хешей двух файлов. Если контрольного ID нет, не создавайте его: нужно сверить фактическую историю.

Проверка владельцем будет так и отмечена в отчёте. Даже после успешного результата остаются проверка совместимого отката и ваше отдельное письменное разрешение на тестовое обновление. C05/E01 закрываются только после ручной приёмки с двух аккаунтов.
