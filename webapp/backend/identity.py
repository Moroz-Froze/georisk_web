"""Identify this project and data directory without exposing filesystem paths."""
import hashlib
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPLICATION = 'georisk-web'


def data_directory():
    return Path(os.environ.get('HAZARD_DATA_DIR', PROJECT_ROOT/'webapp'/'data')).resolve()


def instance_id(directory=None):
    data = Path(directory).resolve() if directory is not None else data_directory()
    identity = os.path.normcase(str(PROJECT_ROOT.resolve()))+'\n'+os.path.normcase(str(data))
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()
