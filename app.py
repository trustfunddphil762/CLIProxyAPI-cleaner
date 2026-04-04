from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, unquote
from wsgiref.simple_server import make_server
from http import cookies

from common import (
    APP_DIR,
    STATIC_DIR,
    CLEANER_LOG_PATH,
    WEB_LOG_PATH,
    REPORT_DIR,
    COOKIE_NAME,
    COOKIE_PATH,
    PASSWORD_PBKDF2_ITERATIONS,
    CLEANER_SERVICE,
    WEB_SERVICE,
    build_cleaner_command,
    ensure_app_dirs,
    load_config,
    save_config,
    sanitize_config_for_ui,
    validate_and_merge_config,
)

SESSION_TTL_SECONDS = 12 * 3600
MAX_BODY_BYTES = 64 * 1024
LOG_TAIL_LINES = 200
MAX_FAILED_ATTEMPTS = 8
FAILED_WINDOW_SECONDS = 10 * 60
LOCKOUT_SECONDS = 15 * 60

FAILED_LOGIN = deque()
SESSIONS: dict[str, dict] = {}


class AppError(Exception):
    def __init__(self, status: str, message: str, http_status: str = '400 Bad Request'):
        super().__init__(message)
        self.status = status
        self.message = message
        self.http_status = http_status


def pbkdf2_hex(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PASSWORD_PBKDF2_ITERATIONS)
    return digest.hex()


def secure_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a), str(b))


def now_ts() -> int:
    return int(time.time())


def client_ip(environ: dict) -> str:
    forwarded = (environ.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    if forwarded:
        return forwarded
    return environ.get('REMOTE_ADDR') or 'unknown'


def prune_login_attempts() -> None:
    cutoff = now_ts() - FAILED_WINDOW_SECONDS
    while FAILED_LOGIN and FAILED_LOGIN[0][0] < cutoff:
        FAILED_LOGIN.popleft()


def is_ip_locked(ip: str) -> bool:
    prune_login_attempts()
    recent = [item for item in FAILED_LOGIN if item[1] == ip]
    if len(recent) < MAX_FAILED_ATTEMPTS:
        return False
    return recent[-1][0] + LOCKOUT_SECONDS > now_ts()


def record_failed_login(ip: str) -> None:
    FAILED_LOGIN.append((now_ts(), ip))
    prune_login_attempts()


def create_session(ip: str) -> str:
    token = __import__('os').urandom(24).hex()
    SESSIONS[token] = {'ip': ip, 'created_at': now_ts(), 'expires_at': now_ts() + SESSION_TTL_SECONDS}
    return token


def get_session(environ: dict):
    cookie_header = environ.get('HTTP_COOKIE') or ''
    jar = cookies.SimpleCookie()
    jar.load(cookie_header)
    morsel = jar.get(COOKIE_NAME)
    if not morsel:
        return None, None
    token = morsel.value
    session = SESSIONS.get(token)
    if not session:
        return token, None
    if session['expires_at'] < now_ts():
        SESSIONS.pop(token, None)
        return token, None
    ip = client_ip(environ)
    if session.get('ip') != ip:
        return token, None
    session['expires_at'] = now_ts() + SESSION_TTL_SECONDS
    return token, session


def require_auth(environ: dict):
    token, session = get_session(environ)
    if not session:
        raise AppError('unauthorized', '请先登录', '401 Unauthorized')
    return token, session


def json_response(start_response, payload: dict, status: str = '200 OK', headers: list | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    base_headers = [
        ('Content-Type', 'application/json; charset=utf-8'),
        ('Content-Length', str(len(body))),
        ('Cache-Control', 'no-store'),
        ('X-Frame-Options', 'DENY'),
        ('X-Content-Type-Options', 'nosniff'),
        ('Referrer-Policy', 'same-origin'),
    ]
    if headers:
        base_headers.extend(headers)
    start_response(status, base_headers)
    return [body]


def text_response(start_response, text: str, status: str = '200 OK', content_type: str = 'text/plain; charset=utf-8', headers: list | None = None):
    body = text.encode('utf-8')
    base_headers = [
        ('Content-Type', content_type),
        ('Content-Length', str(len(body))),
        ('Cache-Control', 'no-store'),
        ('X-Frame-Options', 'DENY'),
        ('X-Content-Type-Options', 'nosniff'),
        ('Referrer-Policy', 'same-origin'),
    ]
    if headers:
        base_headers.extend(headers)
    start_response(status, base_headers)
    return [body]


def parse_json_body(environ: dict) -> dict:
    try:
        length = int(environ.get('CONTENT_LENGTH') or '0')
    except Exception:
        length = 0
    if length <= 0:
        return {}
    if length > MAX_BODY_BYTES:
        raise AppError('payload_too_large', '请求体过大', '413 Payload Too Large')
    raw = environ['wsgi.input'].read(length)
    try:
        data = json.loads(raw.decode('utf-8'))
    except Exception:
        raise AppError('bad_json', 'JSON 解析失败')
    if not isinstance(data, dict):
        raise AppError('bad_json', 'JSON 顶层必须是对象')
    return data


def parse_form_body(environ: dict) -> dict:
    try:
        length = int(environ.get('CONTENT_LENGTH') or '0')
    except Exception:
        length = 0
    if length > MAX_BODY_BYTES:
        raise AppError('payload_too_large', '请求体过大', '413 Payload Too Large')
    raw = environ['wsgi.input'].read(length)
    parsed = parse_qs(raw.decode('utf-8'), keep_blank_values=True)
    return {k: (v[0] if v else '') for k, v in parsed.items()}


def load_static(name: str, content_type: str):
    path = STATIC_DIR / name
    if not path.exists():
        raise AppError('not_found', '静态文件不存在', '404 Not Found')
    return path.read_text(encoding='utf-8'), content_type


def run_command(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = (proc.stdout or '') + (proc.stderr or '')
    return proc.returncode, output.strip()


def systemctl(*args: str) -> tuple[int, str]:
    return run_command(['systemctl', *args])


def read_tail(path: Path, limit_lines: int = LOG_TAIL_LINES) -> str:
    if not path.exists():
        return ''
    try:
        with path.open('r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        return f'读取日志失败: {e}'
    return ''.join(lines[-limit_lines:])


def extract_summary(data: dict) -> dict:
    summary = data.get('summary') if isinstance(data, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    return {
        'deleted_401': int(summary.get('已删除', 0) or 0),
        'disabled_quota': int(summary.get('额度账号已禁用', 0) or 0),
        'revived_enabled': int(summary.get('复活成功启用', 0) or 0),
    }


def list_reports(limit: int = 5) -> list[dict]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(REPORT_DIR.glob('report-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for path in files[:limit]:
        try:
            stat = path.stat()
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        rows.append({
            'name': path.name,
            'size': stat.st_size,
            'mtime': int(stat.st_mtime),
            'summary': extract_summary(data),
        })
    return rows


def read_report_file(name: str) -> dict:
    safe_name = Path(unquote(str(name or ''))).name
    if not safe_name.startswith('report-') or not safe_name.endswith('.json'):
        raise AppError('bad_report_name', '报告文件名不合法')
    path = REPORT_DIR / safe_name
    if not path.exists():
        raise AppError('report_not_found', '报告不存在', '404 Not Found')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        raise AppError('report_read_failed', f'读取报告失败: {e}', '500 Internal Server Error')
    return {
        'name': safe_name,
        'summary': extract_summary(data),
        'data': data,
    }


def cleaner_service_status() -> dict:
    code, output = systemctl('is-active', CLEANER_SERVICE)
    active = output.strip() or ('active' if code == 0 else 'unknown')
    code2, output2 = systemctl('status', CLEANER_SERVICE, '--no-pager', '--lines=40')
    return {'active': active, 'status_text': output2.strip(), 'ok': code2 == 0}


def web_service_status() -> dict:
    code, output = systemctl('is-active', WEB_SERVICE)
    active = output.strip() or ('active' if code == 0 else 'unknown')
    code2, output2 = systemctl('status', WEB_SERVICE, '--no-pager', '--lines=30')
    return {'active': active, 'status_text': output2.strip(), 'ok': code2 == 0}


def build_status_payload() -> dict:
    config = load_config()
    cleaner = cleaner_service_status()
    web = web_service_status()
    preview = build_cleaner_command(config)
    if '--management-key' in preview:
        idx = preview.index('--management-key')
        if idx + 1 < len(preview):
            preview[idx + 1] = '****'
    return {
        'config': sanitize_config_for_ui(config),
        'cleaner_service': cleaner,
        'web_service': web,
        'cleaner_log_tail': read_tail(CLEANER_LOG_PATH),
        'web_log_tail': read_tail(WEB_LOG_PATH),
        'reports': list_reports(),
        'command_preview': preview,
        'auto_refresh_seconds': 8,
    }


def handle_login(environ, start_response):
    if environ.get('REQUEST_METHOD') != 'POST':
        raise AppError('method_not_allowed', '方法不允许', '405 Method Not Allowed')
    ip = client_ip(environ)
    if is_ip_locked(ip):
        raise AppError('rate_limited', '登录失败次数过多，请稍后再试', '429 Too Many Requests')
    body = parse_form_body(environ)
    password = body.get('password', '')
    config = load_config()
    actual = pbkdf2_hex(password, config['password_salt'])
    if not secure_compare(actual, config['password_hash']):
        record_failed_login(ip)
        raise AppError('invalid_password', '密码不正确', '401 Unauthorized')

    token = create_session(ip)
    cookie = cookies.SimpleCookie()
    cookie[COOKIE_NAME] = token
    cookie[COOKIE_NAME]['httponly'] = True
    cookie[COOKIE_NAME]['secure'] = True
    cookie[COOKIE_NAME]['samesite'] = 'Strict'
    cookie[COOKIE_NAME]['path'] = COOKIE_PATH
    cookie[COOKIE_NAME]['max-age'] = str(SESSION_TTL_SECONDS)
    return json_response(start_response, {'ok': True}, headers=[('Set-Cookie', cookie.output(header='').strip())])


def handle_logout(environ, start_response):
    token, _ = get_session(environ)
    if token:
        SESSIONS.pop(token, None)
    cookie = cookies.SimpleCookie()
    cookie[COOKIE_NAME] = ''
    cookie[COOKIE_NAME]['httponly'] = True
    cookie[COOKIE_NAME]['secure'] = True
    cookie[COOKIE_NAME]['samesite'] = 'Strict'
    cookie[COOKIE_NAME]['path'] = COOKIE_PATH
    cookie[COOKIE_NAME]['max-age'] = '0'
    return json_response(start_response, {'ok': True}, headers=[('Set-Cookie', cookie.output(header='').strip())])


def handle_status(environ, start_response):
    require_auth(environ)
    return json_response(start_response, {'ok': True, 'data': build_status_payload()})


def handle_report_detail(environ, start_response):
    require_auth(environ)
    query = parse_qs(environ.get('QUERY_STRING', ''), keep_blank_values=True)
    name = (query.get('name') or [''])[0]
    data = read_report_file(name)
    return json_response(start_response, {'ok': True, 'data': data})


def handle_save_config(environ, start_response):
    require_auth(environ)
    if environ.get('REQUEST_METHOD') != 'POST':
        raise AppError('method_not_allowed', '方法不允许', '405 Method Not Allowed')
    incoming = parse_json_body(environ)
    config = load_config()
    merged = validate_and_merge_config(config, incoming)
    save_config(merged)
    return json_response(start_response, {'ok': True, 'message': '配置已保存，重启 cleaner 后生效', 'data': sanitize_config_for_ui(merged)})


def handle_service_action(environ, start_response, action: str):
    require_auth(environ)
    if environ.get('REQUEST_METHOD') != 'POST':
        raise AppError('method_not_allowed', '方法不允许', '405 Method Not Allowed')
    body = parse_json_body(environ)
    target = body.get('target', 'cleaner')
    if target == 'cleaner':
        service = CLEANER_SERVICE
    elif target == 'web':
        service = WEB_SERVICE
    else:
        raise AppError('bad_target', '不支持的服务目标')

    if action == 'start':
        code, output = systemctl('start', service)
    elif action == 'stop':
        code, output = systemctl('stop', service)
    elif action == 'restart':
        code, output = systemctl('restart', service)
    else:
        raise AppError('bad_action', '不支持的动作')

    status_payload = build_status_payload()
    return json_response(start_response, {
        'ok': code == 0,
        'message': output or 'ok',
        'data': status_payload,
    }, status='200 OK' if code == 0 else '500 Internal Server Error')


def handle_run_once(environ, start_response):
    require_auth(environ)
    if environ.get('REQUEST_METHOD') != 'POST':
        raise AppError('method_not_allowed', '方法不允许', '405 Method Not Allowed')
    config = load_config()
    cmd = build_cleaner_command(config, once=True, dry_run=True)
    code, output = run_command(cmd)
    return json_response(start_response, {
        'ok': code == 0,
        'message': 'dry-run 已执行' if code == 0 else 'dry-run 执行失败',
        'output': output[-20000:],
        'command': cmd,
    }, status='200 OK' if code == 0 else '500 Internal Server Error')


def application(environ, start_response):
    ensure_app_dirs()
    path = environ.get('PATH_INFO', '') or '/'
    host = (environ.get('HTTP_HOST') or '').split(':')[0].lower()
    config = load_config()
    if host and host not in config.get('allowed_hosts', []):
        return json_response(start_response, {'ok': False, 'error': 'forbidden_host'}, status='403 Forbidden')

    try:
        if path in ('/cpa-cleaner', '/cpa-cleaner/'):
            html, content_type = load_static('index.html', 'text/html; charset=utf-8')
            return text_response(start_response, html, content_type=content_type)
        if path == '/cpa-cleaner/app.js':
            js, content_type = load_static('app.js', 'application/javascript; charset=utf-8')
            return text_response(start_response, js, content_type=content_type)
        if path == '/cpa-cleaner/styles.css':
            css, content_type = load_static('styles.css', 'text/css; charset=utf-8')
            return text_response(start_response, css, content_type=content_type)
        if path == '/cpa-cleaner/api/login':
            return handle_login(environ, start_response)
        if path == '/cpa-cleaner/api/logout':
            return handle_logout(environ, start_response)
        if path == '/cpa-cleaner/api/status':
            return handle_status(environ, start_response)
        if path == '/cpa-cleaner/api/report':
            return handle_report_detail(environ, start_response)
        if path == '/cpa-cleaner/api/config/save':
            return handle_save_config(environ, start_response)
        if path == '/cpa-cleaner/api/service/start':
            return handle_service_action(environ, start_response, 'start')
        if path == '/cpa-cleaner/api/service/stop':
            return handle_service_action(environ, start_response, 'stop')
        if path == '/cpa-cleaner/api/service/restart':
            return handle_service_action(environ, start_response, 'restart')
        if path == '/cpa-cleaner/api/run-once':
            return handle_run_once(environ, start_response)
        return json_response(start_response, {'ok': False, 'error': 'not_found'}, status='404 Not Found')
    except AppError as e:
        return json_response(start_response, {'ok': False, 'error': e.status, 'message': e.message}, status=e.http_status)
    except Exception as e:
        return json_response(start_response, {'ok': False, 'error': 'internal_error', 'message': str(e)}, status='500 Internal Server Error')


def main():
    config = load_config()
    host = str(config.get('listen_host', '127.0.0.1'))
    port = int(config.get('listen_port', 28717))
    WEB_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with WEB_LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] starting web console on {host}:{port}\n')
    httpd = make_server(host, port, application)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
