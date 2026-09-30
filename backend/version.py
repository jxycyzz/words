import hashlib
from pathlib import Path

VERSION='0.2.15'


def build_id():
    index=Path(__file__).resolve().parents[1]/'frontend/dist/index.html'
    return hashlib.sha256(index.read_bytes()).hexdigest()[:16] if index.exists() else 'unbuilt'
