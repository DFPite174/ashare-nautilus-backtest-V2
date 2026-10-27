"""Reject ABI/binary drift before importing the version-pinned native bridge."""
from functools import lru_cache
from pathlib import Path
import hashlib, importlib.metadata, json, sys

@lru_cache(maxsize=1)
def load_bridge():
    root = Path(__file__).resolve().parent
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12) or importlib.metadata.version('nautilus_trader') != '1.231.0':
        raise RuntimeError('Split bridge requires pinned Windows x64 Python3.12 NT1.231.0')
    import nautilus_trader
    package = Path(nautilus_trader.__file__).parent
    manifest = json.loads((root / 'SPLIT_BUILD.json').read_text())
    for path in (root / 'split_native.pyx', root / 'split_native.cp312-win_amd64.pyd', package / 'model/position.cp312-win_amd64.pyd', package / 'model/position.pxd'):
        expected = [d for name, d in manifest['sha256'].items() if Path(name).name == path.name]
        raw = path.read_bytes()
        # 兼容 Windows 跨平台检出：文本源码文件做换行符标准化
        if path.name.endswith(('.pyx', '.pxd', '.txt', '.json')):
            raw = raw.replace(b'\r\n', b'\n')
        if len(expected) != 1 or hashlib.sha256(raw).hexdigest() != expected[0]:
            raise RuntimeError('Split binary/ABI fingerprint changed: ' + path.name)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import split_native
    return split_native
