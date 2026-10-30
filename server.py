import io
import json
import os
import secrets
import threading
import webbrowser
import zipfile
import socket
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'candidates.json'
VACANCIES = ROOT / 'data' / 'vacancies.json'
LOCK = threading.Lock()
TOKEN = secrets.token_urlsafe(32)
SESSIONS = {}
LOGIN_ATTEMPTS = {}
ACCESS_KEY = ''

def merge_candidates(base, desired, actual):
    """Apply only changed cards. Never replace a concurrently edited card."""
    validate(base)
    validate(desired)
    before = {c['id']: c for c in base}
    after = {c['id']: c for c in desired}
    live = {c['id']: c for c in actual}
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    conflicts = [key for key in changed if live.get(key) != before.get(key) and live.get(key) != after.get(key)]
    if conflicts:
        return None, [str((live.get(key) or before.get(key) or after[key]).get('name') or 'Без имени') for key in conflicts]
    for key in changed:
        if key in after:
            live[key] = after[key]
        else:
            live.pop(key, None)
    return list(live.values()), []

LOGIN_PAGE = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход · InterviewFlow</title><link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2032%2032%22%3E%3Crect%20width%3D%2232%22%20height%3D%2232%22%20rx%3D%227%22%20fill%3D%22%23f4f5f7%22%2F%3E%3Cg%20fill%3D%22%23292e36%22%3E%3Ccircle%20cx%3D%229%22%20cy%3D%229%22%20r%3D%224%22%2F%3E%3Cpath%20d%3D%22M5%2015h7l3%207h5v4h-8l-3-7H5z%22%2F%3E%3C%2Fg%3E%3Cg%20fill%3D%22%232865dc%22%3E%3Ccircle%20cx%3D%2223%22%20cy%3D%229%22%20r%3D%224%22%2F%3E%3Cpath%20d%3D%22M20%2015h7v10h-4v-6h-2l-3%204-3-2z%22%2F%3E%3C%2Fg%3E%3C%2Fsvg%3E"><style>body{font:16px Segoe UI,sans-serif;background:#f4f6f3;color:#243331;margin:0;display:grid;place-items:center;min-height:100vh}form{background:white;border:1px solid #dce3dd;border-radius:12px;padding:30px;max-width:360px;margin:20px}input,button{box-sizing:border-box;width:100%;padding:12px;font:inherit;margin-top:12px;border:1px solid #ccd6d0;border-radius:7px}button{background:#285c48;color:white;cursor:pointer}p{line-height:1.5}#error{color:#a52c24}</style><form><h1>Собеседования</h1><p>Введите PIN из четырёх цифр из окна запуска на основном компьютере.</p><label for="key">PIN-код</label><input id="key" type="password" inputmode="numeric" pattern="[0-9]{4}" minlength="4" maxlength="4" autocomplete="current-password" required><button>Войти</button><p id="error" role="status"></p></form><script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:document.querySelector('#key').value})});const d=await r.json();if(!r.ok)throw Error(d.error);location.reload()}catch(e){document.querySelector('#error').textContent=e.message}};</script></html>'''

def default_vacancy_id():
    private_seed = ROOT / 'default-vacancy.json'
    if private_seed.exists():
        return json.loads(private_seed.read_text(encoding='utf-8'))['id']
    return 'general-interview'


def read_data():
    if not DATA.exists():
        return []
    items = json.loads(DATA.read_text(encoding='utf-8'))
    for item in items:
        item.setdefault('vacancyId', default_vacancy_id())
    return items

def write_data(items):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    temporary = DATA.with_suffix('.tmp')
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, DATA)

def read_vacancies():
    if VACANCIES.exists():
        return validate_vacancies(json.loads(VACANCIES.read_text(encoding='utf-8')))
    seed = ROOT / 'default-vacancy.json'
    if not seed.exists():
        seed = ROOT / 'default-template.json'
    return validate_vacancies([json.loads(seed.read_text(encoding='utf-8'))])


def write_vacancies(items):
    VACANCIES.parent.mkdir(parents=True, exist_ok=True)
    temporary = VACANCIES.with_suffix('.tmp')
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, VACANCIES)


def validate_vacancies(items):
    if not isinstance(items, list) or not 0 <= len(items) <= 200:
        raise ValueError('Неверный список вакансий')
    ids = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or len(item['id']) > 100 or item['id'] in ids:
            raise ValueError('Неверный идентификатор вакансии')
        ids.add(item['id'])
        if not isinstance(item.get('name'), str) or not item['name'].strip() or len(item['name']) > 200:
            raise ValueError('Укажите название вакансии до 200 символов')
        sections = item.get('sections')
        if not isinstance(sections, list) or len(sections) != 2:
            raise ValueError('Нужны разделы знакомства и профильных вопросов')
        question_ids = set()
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get('name'), str) or not isinstance(section.get('description'), str):
                raise ValueError('Неверный раздел вопросов')
            questions = section.get('questions')
            if not isinstance(questions, list) or len(questions) > 300:
                raise ValueError('Слишком много вопросов')
            for q in questions:
                if not isinstance(q, list) or len(q) not in (4, 5) or any(not isinstance(v, str) or len(v) > 20000 for v in q[:4]):
                    raise ValueError('Неверный вопрос')
                if not q[1] or q[1] in question_ids or not q[2].strip() or (len(q) == 5 and not isinstance(q[4], bool)):
                    raise ValueError('Неверный или повторный вопрос')
                question_ids.add(q[1])
        criteria = item.get('criteria')
        if not isinstance(criteria, list) or len(criteria) > 100:
            raise ValueError('Неверный список оценок')
        criterion_ids = set()
        for criterion in criteria:
            if not isinstance(criterion, dict) or any(not isinstance(criterion.get(k), str) or len(criterion[k]) > 20000 for k in ('id', 'name', 'guide')):
                raise ValueError('Неверный критерий оценки')
            if not criterion['id'] or not criterion['name'].strip() or criterion['id'] in criterion_ids or ('archived' in criterion and not isinstance(criterion['archived'], bool)):
                raise ValueError('Неверный или повторный критерий')
            criterion_ids.add(criterion['id'])
    return items


def merge_vacancies(base, desired, actual):
    validate_vacancies(base)
    validate_vacancies(desired)
    before = {v['id']: v for v in base}
    after = {v['id']: v for v in desired}
    live = {v['id']: v for v in actual}
    changed = [key for key in before.keys() | after.keys() if before.get(key) != after.get(key)]
    if any(key not in after for key in changed):
        raise ValueError('Удаление вакансий не поддерживается')
    if any(live.get(key) != before.get(key) and live.get(key) != after.get(key) for key in changed):
        return None
    for key in changed:
        live[key] = after[key]
    result = list(live.values())
    validate_vacancies(result)
    return result


def validate(items):
    if not isinstance(items, list) or len(items) > 10000:
        raise ValueError('Неверный формат резервной копии')
    ids = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in ids:
            raise ValueError('Неверная или повторная карточка')
        ids.add(item['id'])
        for key in ('name', 'phone', 'email', 'hh', 'date', 'experience', 'status', 'review', 'strengths', 'concerns', 'next', 'vacancyId'):
            if key in item and not isinstance(item[key], str):
                raise ValueError('Неверное значение поля')
        for key in ('answers', 'notes', 'scores', 'evidence'):
            if key in item and (not isinstance(item[key], dict) or any(not isinstance(v, str) for v in item[key].values())):
                raise ValueError('Неверный формат ответов')
        if 'skipped' in item and (not isinstance(item['skipped'], dict) or any(not isinstance(v, bool) for v in item['skipped'].values())):
            raise ValueError('Неверный формат пропущенных вопросов')
    return items

def docx(lines):
    paragraphs = ''.join('<w:p><w:r><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="22"/></w:rPr><w:t xml:space="preserve">' + escape(part) + '</w:t></w:r></w:p>' for line in lines for part in (line.splitlines() or ['']))
    document = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + paragraphs + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr></w:body></w:document>'
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr('word/document.xml', document)
    return out.getvalue()

class Handler(BaseHTTPRequestHandler):
    def authenticated(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            key = cookie['interview_session'].value
            return SESSIONS.get(key, 0) > time.time()
        except (KeyError, ValueError):
            return False

    def log_message(self, *args):
        pass

    def respond(self, payload, kind='application/json; charset=utf-8', status=200, cookie=None):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == '/':
            if not self.authenticated():
                return self.respond(LOGIN_PAGE.encode('utf-8'), 'text/html; charset=utf-8')
            page = (ROOT / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', TOKEN)
            self.respond(page.encode('utf-8'), 'text/html; charset=utf-8')
        elif self.path == '/api/vacancies' and self.authenticated() and self.headers.get('X-App-Token') == TOKEN:
            try:
                with LOCK:
                    self.respond(read_vacancies())
            except Exception:
                self.respond({'error': 'Не удалось прочитать шаблоны вакансий. Проверьте data/vacancies.json.'}, status=500)
        elif self.path == '/api/candidates' and self.authenticated() and self.headers.get('X-App-Token') == TOKEN:
            try:
                with LOCK:
                    self.respond(read_data())
            except Exception:
                self.respond({'error': 'Не удалось прочитать анкеты. Проверьте файл data/candidates.json; он не перезаписан.'}, status=500)
        else:
            self.respond({'error': 'Недоступно'}, status=404)

    def do_POST(self):
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            return self.respond({'error': 'Нет доступа'}, status=403)
        if self.path != '/api/login' and (not self.authenticated() or self.headers.get('X-App-Token') != TOKEN):
            return self.respond({'error': 'Сеанс завершён. Скачайте резервную копию несохранённых ответов и войдите заново.'}, status=401)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 20_000_000:
                raise ValueError('Размер данных недопустим')
            payload = json.loads(self.rfile.read(length))
            if self.path == '/api/login':
                now = time.time()
                ip = self.client_address[0]
                with LOCK:
                    attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < 60]
                    if len(attempts) >= 10:
                        return self.respond({'error': 'Слишком много попыток. Подождите минуту.'}, status=429)
                    if not isinstance(payload, dict) or not isinstance(payload.get('key'), str) or not secrets.compare_digest(payload['key'].encode('utf-8'), ACCESS_KEY.encode('utf-8')):
                        LOGIN_ATTEMPTS[ip] = attempts + [now]
                        return self.respond({'error': 'Неверный код доступа'}, status=403)
                    session = secrets.token_urlsafe(32)
                    SESSIONS[session] = now + 12 * 3600
                    LOGIN_ATTEMPTS.pop(ip, None)
                return self.respond({'ok': True}, cookie=f'interview_session={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')
            if self.path == '/api/candidates':
                if not isinstance(payload, dict) or 'base' not in payload or 'items' not in payload:
                    raise ValueError('Обновите страницу: установлен новый формат сохранения')
                with LOCK:
                    actual = read_data()
                    validate(payload['items'])
                    vacancy_ids = {v['id'] for v in read_vacancies()}
                    existing = {c['id']: c for c in actual}
                    for candidate in payload['items']:
                        vacancy_id = candidate.get('vacancyId', default_vacancy_id())
                        if vacancy_id not in vacancy_ids:
                            raise ValueError('Выбранная вакансия не найдена. Обновите список вакансий.')
                        previous = existing.get(candidate['id'])
                        if previous and previous.get('vacancyId', default_vacancy_id()) != vacancy_id:
                            raise ValueError('Анкета уже принадлежит другой вакансии. Создайте нового кандидата в выбранной вакансии.')
                    items, conflicts = merge_candidates(payload['base'], payload['items'], actual)
                    if conflicts:
                        return self.respond({'error': 'Анкету уже изменили на другом компьютере: ' + ', '.join(conflicts) + '. Ваши изменения остались в браузере. Скачайте резервную копию, затем обновите список.', 'conflict': True}, status=409)
                    write_data(items)
                self.respond({'saved': datetime.now().isoformat()})
            elif self.path == '/api/vacancies/order':
                ids = payload.get('ids') if isinstance(payload, dict) else None
                with LOCK:
                    vacancies = read_vacancies()
                    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) != len(vacancies) or set(ids) != {v['id'] for v in vacancies}:
                        return self.respond({'error': 'Список вакансий изменился. Обновите список и повторите.', 'conflict': True}, status=409)
                    by_id = {v['id']: v for v in vacancies}
                    vacancies = [by_id[i] for i in ids]
                    write_vacancies(vacancies)
                self.respond({'items': vacancies})
            elif self.path == '/api/vacancies/delete':
                if not isinstance(payload, dict) or payload.get('confirmed') is not True or payload.get('confirmedAgain') is not True:
                    raise ValueError('Нужны оба подтверждения удаления')
                vacancy_id = payload.get('id')
                with LOCK:
                    original_vacancies = read_vacancies()
                    original_candidates = read_data()
                    target = next((v for v in original_vacancies if v['id'] == vacancy_id), None)
                    target_candidates = [c for c in original_candidates if c.get('vacancyId', default_vacancy_id()) == vacancy_id]
                    base_candidates = payload.get('baseCandidates')
                    if not isinstance(base_candidates, list):
                        raise ValueError('Неверный список анкет для удаления')
                    validate(base_candidates)
                    if target is None or target != payload.get('baseVacancy') or {c['id']: c for c in target_candidates} != {c['id']: c for c in base_candidates}:
                        return self.respond({'error': 'Вакансия или её анкеты изменились на другом компьютере. Обновите список и повторите удаление.', 'conflict': True}, status=409)
                    vacancies = [v for v in original_vacancies if v['id'] != vacancy_id]
                    candidates = [c for c in original_candidates if c.get('vacancyId', default_vacancy_id()) != vacancy_id]
                    write_vacancies(vacancies)
                    try:
                        write_data(candidates)
                    except Exception:
                        write_vacancies(original_vacancies)
                        raise
                self.respond({'vacancies': vacancies, 'candidates': candidates})
            elif self.path == '/api/restore':
                if not isinstance(payload, dict):
                    raise ValueError('Неверный формат резервной копии')
                with LOCK:
                    original_candidates = read_data()
                    original_vacancies = read_vacancies()
                    candidates, conflicts = merge_candidates(payload.get('baseCandidates'), payload.get('candidates'), original_candidates)
                    vacancies = merge_vacancies(payload.get('baseVacancies'), payload.get('vacancies'), original_vacancies)
                    if conflicts or vacancies is None:
                        return self.respond({'error': 'Анкеты или шаблоны уже изменены на другом компьютере. Обновите список и повторите восстановление.', 'conflict': True}, status=409)
                    vacancy_ids = {v['id'] for v in vacancies}
                    if any(c.get('vacancyId', default_vacancy_id()) not in vacancy_ids for c in candidates):
                        raise ValueError('В копии есть анкета с неизвестной вакансией')
                    write_vacancies(vacancies)
                    try:
                        write_data(candidates)
                    except Exception:
                        write_vacancies(original_vacancies)
                        raise
                self.respond({'candidates': candidates, 'vacancies': vacancies})
            elif self.path == '/api/vacancies':
                if not isinstance(payload, dict) or 'base' not in payload or 'items' not in payload:
                    raise ValueError('Неверный формат шаблонов вакансий')
                with LOCK:
                    vacancies = merge_vacancies(payload['base'], payload['items'], read_vacancies())
                    if vacancies is None:
                        return self.respond({'error': 'Шаблон этой вакансии уже изменён на другом компьютере. Скопируйте свои правки, обновите список и повторите сохранение.', 'conflict': True}, status=409)
                    write_vacancies(vacancies)
                self.respond({'ok': True, 'items': vacancies})
            elif self.path == '/api/docx':
                lines = payload.get('lines')
                if not isinstance(lines, list) or any(not isinstance(s, str) for s in lines):
                    raise ValueError('Неверный отчёт')
                self.respond(docx(lines), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
            else:
                self.respond({'error': 'Не найдено'}, status=404)
        except (ValueError, TypeError) as e:
            self.respond({'error': str(e)}, status=400)
        except Exception:
            self.respond({'error': 'Не удалось сохранить данные. Скачайте резервную копию и проверьте доступ к папке.'}, status=500)

if __name__ == '__main__':
    DATA.parent.mkdir(parents=True, exist_ok=True)
    # A Windows file lock also prevents two hosts from writing the same shared folder.
    instance_lock = open(DATA.parent / 'server.lock', 'a+b')
    try:
        instance_lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            if instance_lock.read(1) == b'':
                instance_lock.write(b'0')
                instance_lock.flush()
            instance_lock.seek(0)
            msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit('Эта папка анкет уже открыта другим сервером. Используйте его сетевой адрес.')
    config_path = DATA.parent / 'network.json'
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding='utf-8'))
    else:
        config = {'port': 8760}
    pin = config.get('access_key')
    if not isinstance(pin, str) or len(pin) != 4 or not pin.isascii() or not pin.isdigit():
        config['access_key'] = f'{secrets.randbelow(10000):04d}'
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    ACCESS_KEY = config['access_key']
    try:
        server = ThreadingHTTPServer(('0.0.0.0', int(config['port'])), Handler)
    except OSError:
        raise SystemExit('Не удалось открыть сетевой порт. Возможно, приложение уже запущено. Проверьте окно запуска.')
    hostname = socket.gethostname()
    url = f'http://127.0.0.1:{server.server_port}'
    print('На этом компьютере: ' + url, flush=True)
    print(f'На других компьютерах: http://{hostname}:{server.server_port}', flush=True)
    try:
        addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(hostname, None, socket.AF_INET) if not entry[4][0].startswith('127.')})
        for address in addresses:
            print(f'Адрес по IP: http://{address}:{server.server_port}', flush=True)
    except OSError:
        pass
    print('PIN-КОД ДОСТУПА: ' + ACCESS_KEY, flush=True)
    print('Запускайте приложение только на одном компьютере. Остальные открывают адрес в браузере.', flush=True)
    print('Если нет подключения, разрешите Python в брандмауэре Windows для частной рабочей сети.', flush=True)
    print('Для завершения закройте это окно. Анкеты сохранены в папке data.', flush=True)
    if os.environ.get('INTERVIEW_NO_BROWSER') != '1':
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
