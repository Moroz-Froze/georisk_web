"""HTTP boundary controls, independent of scientific model code."""
import logging
import os
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

JSON_LIMIT = 128 * 1024
UPLOAD_LIMIT = 201 * 1024 * 1024  # one 200 MiB file plus bounded multipart overhead
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https://tile.openstreetmap.org; "
       "connect-src 'self' https://tile.openstreetmap.org; worker-src 'self' blob:; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
LOGGER = logging.getLogger('uvicorn.error')


def configured_hosts():
    hosts = {h.strip().lower() for h in os.environ.get('GEORISK_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]').split(',') if h.strip()}
    if not hosts or any(h == '*' or '/' in h or '@' in h or h.startswith('*.') for h in hosts):
        raise RuntimeError('GEORISK_ALLOWED_HOSTS must list exact hostnames, without schemes, ports or wildcards.')
    return hosts


def valid_host(value, allowed):
    try:
        parsed = urlsplit('//'+value)
        hostname = '['+parsed.hostname+']' if ':' in (parsed.hostname or '') else parsed.hostname
        _ = parsed.port
        return (value == value.strip() and hostname in allowed and parsed.username is None and parsed.password is None
                and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app
        self.hosts = configured_hosts()
        self.public_origin = os.environ.get('GEORISK_PUBLIC_ORIGIN', '').rstrip('/')
        if self.public_origin:
            parsed = urlsplit(self.public_origin)
            if parsed.scheme not in ('http', 'https') or parsed.path or not valid_host(parsed.netloc, self.hosts):
                raise RuntimeError('GEORISK_PUBLIC_ORIGIN must be an origin on GEORISK_ALLOWED_HOSTS.')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        request_id = uuid4().hex
        scope.setdefault('state', {})['request_id'] = request_id
        started = False

        async def secured_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                response_headers = MutableHeaders(scope=message)
                for key, value in {
                    'Content-Security-Policy': CSP, 'X-Content-Type-Options':'nosniff', 'X-Frame-Options':'DENY',
                    'Referrer-Policy':'strict-origin-when-cross-origin', 'Permissions-Policy':'camera=(), microphone=(), geolocation=()',
                    'Cross-Origin-Opener-Policy':'same-origin', 'Cross-Origin-Resource-Policy':'same-origin',
                    'X-Request-ID':request_id,
                }.items():
                    response_headers.setdefault(key, value)
                if scope['scheme'] == 'https' or self.public_origin.startswith('https://'):
                    response_headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
            await send(message)

        async def reject(status, detail):
            # Do not log cookies, CSRF values, query strings, filenames or bodies.
            LOGGER.warning('security_rejection status=%s request_id=%s', status, request_id)
            response = JSONResponse({'detail':detail, 'request_id':request_id}, status_code=status,
                                    headers={'Cache-Control':'no-store'})
            await response(scope, receive, secured_send)

        if len(headers.getlist('host')) != 1 or not valid_host(headers.get('host', ''), self.hosts):
            return await reject(400, 'Недопустимый адрес сервера.')
        if scope['method'] not in ('GET', 'HEAD', 'OPTIONS'):
            origin = headers.get('origin')
            expected = self.public_origin or scope['scheme']+'://'+headers['host']
            if (origin is not None and origin != expected) or headers.get('sec-fetch-site') == 'cross-site':
                return await reject(403, 'Запрос с другого сайта отклонён.')
        limit = UPLOAD_LIMIT if scope['path'] == '/api/uploads' else JSON_LIMIT
        lengths = headers.getlist('content-length')
        if lengths and (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0]) > 20):
            return await reject(400, 'Некорректный размер запроса.')
        if lengths and int(lengths[0]) > limit:
            return await reject(413, 'Превышен допустимый размер запроса.')
        range_header = headers.get('range', '')
        if len(range_header) > 256 or range_header.count(',') > 3:
            return await reject(416, 'Слишком сложный диапазон файла.')
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message['type'] == 'http.request':
                consumed += len(message.get('body', b''))
                if consumed > limit:
                    raise HTTPException(413, 'Превышен допустимый размер запроса.')
            return message

        try:
            await self.app(scope, bounded_receive, secured_send)
        except Exception:
            if started:
                raise
            LOGGER.exception('request_failure request_id=%s', request_id)
            await reject(500, 'Не удалось обработать запрос. Сообщите код ошибки администратору.')
