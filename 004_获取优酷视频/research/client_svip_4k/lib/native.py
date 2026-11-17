"""研究入口兼容导入；实际实现统一位于优酷公共模块。"""
import sys
from local.youku import native as _implementation
sys.modules[__name__] = _implementation
