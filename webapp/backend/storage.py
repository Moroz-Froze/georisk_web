import json
import time
from pathlib import Path
from uuid import UUID, uuid4


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    # Windows readers and antivirus may briefly hold the destination open.
    for attempt in range(20):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(.01*(attempt+1))


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def identifier(value):
    try:
        return str(UUID(str(value)))
    except ValueError as exc:
        raise FileNotFoundError('Объект не найден.') from exc


def asset_path(root, asset_id, suffixes=None):
    folder = Path(root) / 'uploads' / identifier(asset_id)
    meta = read_json(folder/'asset.json')
    if suffixes and meta['suffix'] not in suffixes:
        raise ValueError('Неверный формат входного файла: ' + meta['name'])
    return folder / ('data' + meta['suffix'])
