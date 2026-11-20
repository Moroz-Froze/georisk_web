"""Start one web server on the requested address, or open the existing instance."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import socket
import sys
import traceback
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.backend.identity import APPLICATION, data_directory, instance_id


class StartupError(RuntimeError):
    pass


def browser_url(host: str, port: int) -> str:
    if host in ('0.0.0.0', '::'):
        host = '127.0.0.1'
    if ':' in host:
        host = f'[{host}]'
    return f'http://{host}:{port}/'


async def launch_browser(url):
    try:
        await asyncio.to_thread(webbrowser.open, url)
    except Exception as exc:
        print(f'Open {url} in your browser ({exc}).', file=sys.stderr)


async def open_when_ready(server, url):
    while not server.started and not server.should_exit:
        await asyncio.sleep(.1)
    if server.started:
        await launch_browser(url)


def existing_server(url):
    """None means still starting; false means a different service/instance."""
    try:
        request = Request(url+'api/health', headers={'Accept': 'application/json'})
        with build_opener(ProxyHandler({})).open(request, timeout=.5) as response:
            health = json.loads(response.read(16384))
        return isinstance(health, dict) and health.get('status') == 'ok' and health.get('application') == APPLICATION and health.get('instance') == instance_id()
    except (URLError, TimeoutError, ConnectionError):
        return None
    except (ValueError, OSError):
        return False


def reserve_address(host, port):
    listener = socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == 'win32':
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.setblocking(False)
        return listener
    except OSError:
        listener.close()
        raise


async def serve(host, port, open_browser=False):
    url = browser_url(host, port)
    # Reserve the address before importing the app or running queue recovery.
    try:
        listener = reserve_address(host, port)
    except OSError as exc:
        for _ in range(10):
            match = await asyncio.to_thread(existing_server, url)
            if match:
                print(f'GeoRisk is already running: {url}', flush=True)
                if open_browser:
                    await launch_browser(url)
                return
            if match is False:
                break
            await asyncio.sleep(.15)
        raise StartupError(f'Address {host}:{port} is in use or unavailable. It is not this GeoRisk instance. '
                           'Close the other application or choose a different port with start.cmd -Port 8080.') from exc

    task = None
    handler = None
    try:
        import uvicorn
        server = uvicorn.Server(uvicorn.Config('webapp.backend.main:app', host=host, port=port, workers=1,
            limit_concurrency=64, timeout_keep_alive=5, server_header=False,
            forwarded_allow_ips=os.environ.get('GEORISK_TRUSTED_PROXIES', '127.0.0.1')))
        directory = data_directory()
        directory.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        handler = RotatingFileHandler(directory/'server.log', maxBytes=10*1024*1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        for name in ('uvicorn', 'uvicorn.access'):
            logging.getLogger(name).addHandler(handler)
        if open_browser:
            task = asyncio.create_task(open_when_ready(server, url))
        await server.serve(sockets=[listener])
    finally:
        listener.close()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if handler is not None:
            for name in ('uvicorn', 'uvicorn.access'):
                logging.getLogger(name).removeHandler(handler)
            handler.close()


def startup_failure(message):
    path = data_directory()/'server.log'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(f'{datetime.now(timezone.utc).isoformat()} STARTUP ERROR\n{message}\n')
    except OSError:
        pass
    print(message, file=sys.stderr)
    print(f'Server log: {path}', file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(description='GeoRisk web server')
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    parser.add_argument('--open-browser', action='store_true')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if args.test:
        import pytest
        return pytest.main([str(ROOT/'webapp'/'tests'), '-q'])
    try:
        asyncio.run(serve(args.host, args.port, args.open_browser))
    except StartupError as exc:
        return startup_failure(str(exc))
    except Exception:
        return startup_failure(traceback.format_exc())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
