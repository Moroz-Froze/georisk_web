"""Local entry point; use a Python installation with GDAL and NumPy."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'webapp' / '.deps'), str(ROOT)]
os.environ['PYTHONPATH'] = os.pathsep.join([str(ROOT / 'webapp' / '.deps'), str(ROOT), os.environ.get('PYTHONPATH', '')])

if __name__ == '__main__':
    if '--test' in sys.argv:
        import pytest
        raise SystemExit(pytest.main([str(ROOT/'webapp'/'tests'), '-q']))
    import uvicorn
    uvicorn.run('webapp.backend.main:app', host=os.environ.get('HOST', '127.0.0.1'),
                port=int(os.environ.get('PORT', '8000')), workers=1)
