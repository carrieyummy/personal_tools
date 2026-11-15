"""区分联网来源的认证、签名失效、来源变化及未适配媒体错误。"""


class PlaybackError(ValueError):
    """表示网络来源不可用或协议未验证；文字不得包含凭据或签名地址。"""


class AuthenticationError(PlaybackError):
    """表示服务器拒绝登录态，需要用户在客户端重新登录。"""


class AddressExpired(PlaybackError):
    """表示媒体签名地址失效，允许重新解析同一来源。"""


class SourceChanged(PlaybackError):
    """表示来源身份变化，禁止将旧断点拼接到新来源。"""
