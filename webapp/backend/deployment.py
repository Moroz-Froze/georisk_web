"""Public URL prefix: A reverse proxy removes it before forwarding to the application."""
import os
import re


def public_base_path():
    value = os.environ.get('GEORISK_BASE_PATH', '').rstrip('/')
    if value and not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)+', value):
        raise ValueError('GEORISK_BASE_PATH must contain simple absolute URL segments, e.g. /georisk.')
    return value
