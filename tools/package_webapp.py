"""Package the application and its unchanged model sources, without user data."""
from pathlib import Path
import hashlib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT/'dist'/'georisk_web-1.0.0.zip'
EXCLUDED = {'.deps', 'node_modules', 'data', '__pycache__', 'test-results', 'playwright-report'}


def sources():
    for directory in ('webapp', 'avalanche_scenario', 'landslide_scenario', 'wildfire_scenario'):
        for path in (ROOT/directory).rglob('*'):
            relative = path.relative_to(ROOT)
            preview = path.suffix == '.png' and 'icons' not in relative.parts
            if path.is_file() and not (EXCLUDED & set(relative.parts)) and not preview and path.suffix not in ('.pyc', '.tsbuildinfo', '.log'):
                yield path, relative
    for filename in ('compose.yaml', '.dockerignore', '.gitignore'):
        yield ROOT/filename, Path(filename)
    yield ROOT/'webapp'/'README.md', Path('README.md')
    yield Path(__file__), Path('tools/package_webapp.py')


if __name__ == '__main__':
    OUTPUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUTPUT, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, relative in sources():
            archive.write(path, relative)
    with zipfile.ZipFile(OUTPUT) as archive:
        assert archive.testzip() is None
        assert 'webapp/frontend/dist/index.html' in archive.namelist()
        assert all(f'webapp/frontend/dist/icons/{name}.png' in archive.namelist() for name in ('avalanche', 'landslide', 'wildfire'))
        assert 'wildfire_scenario/third_party/cffdrs/UPSTREAM.md' in archive.namelist()
        assert not any('/data/' in name or '/node_modules/' in name or '/.deps/' in name for name in archive.namelist())
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    OUTPUT.with_suffix('.sha256').write_text(digest+'  '+OUTPUT.name+'\n', encoding='utf-8')
    print(OUTPUT)
    print(f'{OUTPUT.stat().st_size:,} bytes; SHA256 {digest}')
