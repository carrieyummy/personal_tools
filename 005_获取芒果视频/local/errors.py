"""定义可脱敏报告的任务错误、批次认证错误及取消信号。"""


class ToolError(Exception):
    """表示可直接向用户显示的错误；构造参数为不含凭据的错误说明。"""


class AuthenticationError(ToolError):
    """表示登录态失效；停止本批次新任务，构造参数为安全的错误说明。"""


class PlaybackDenied(ToolError):
    """表示单集无完整播放权限或未上线；构造参数为业务状态说明。"""


class AddressExpired(ToolError):
    """表示媒体地址需要重新解析；构造参数只包含 HTTP 状态，不包含原地址。"""


class SourceChanged(ToolError):
    """表示刷新后的源版本改变；保留现场，在配置重试上限内重新建立新版本计划。"""


class Cancelled(ToolError):
    """表示收到取消信号；构造参数为当前阶段的安全说明。"""
