"""研究模块；将工具根加入路径以复用正式公共接口。"""
from pathlib import Path
import sys
_root = str(Path(__file__).resolve().parents[3])
if _root not in sys.path:
    sys.path.insert(0, _root)
