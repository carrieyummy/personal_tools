"""研究兼容入口；原生版本校验与隔离调用实现统一在正式模块。"""
import sys
from local.youku import key_bridge as _implementation
sys.modules[__name__] = _implementation
