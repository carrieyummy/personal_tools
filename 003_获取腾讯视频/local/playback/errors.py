"""区分联网来源的认证、签名失效、来源变化及未适配媒体错误。"""


class PlaybackError(ValueError):
    """表示网络来源不可用或协议未验证；文字不得包含凭据或签名地址。"""


class AuthenticationError(PlaybackError):
    """表示客户端登录读取或有界认证恢复失败；错误文字不得包含登录票据。"""


class AuthenticationRejected(AuthenticationError):
    """表示单次请求明确拒绝认证，仅由登录管理器消费并尝试恢复。"""

    def __init__(self, details: dict):
        """保存已脱敏的协议诊断 details；生成不含请求内容的异常，无返回值。"""
        super().__init__("腾讯未接受本次请求的客户端登录态")
        self.details = details


class AuthenticationStopped(AuthenticationError):
    """表示其他任务已耗尽认证恢复机会，当前任务保留现场并停止等待。"""


class ApiHTTPError(PlaybackError):
    """表示接口 HTTP 失败；与可续签的媒体地址过期分开处理。"""

    def __init__(self, status: int):
        """保存 HTTP 状态整数 status；返回无地址或响应正文的错误对象。"""
        super().__init__(f"接口 HTTP 请求失败：{status}")
        self.status = status


class AddressExpired(PlaybackError):
    """表示媒体签名地址失效，允许重新解析同一来源。"""


class SourceChanged(PlaybackError):
    """表示来源身份变化，禁止将旧断点拼接到新来源。"""
