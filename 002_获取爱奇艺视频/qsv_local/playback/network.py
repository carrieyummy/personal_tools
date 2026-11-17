"""研究请求的共享网络边界：代理取自配置，带会话头的请求禁止跨域跳转。"""

from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class SameHostRedirect(HTTPRedirectHandler):
    """限制携带原生请求头的重放仅在原主机跳转，防止票据被带到另一主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """返回同域跳转请求；req 为原请求、fp 为响应流、code/msg 为状态码/文字、headers 为响应头、newurl 为目标地址。"""
        if urlsplit(req.full_url).hostname != urlsplit(newurl).hostname:
            raise RuntimeError("携带播放会话头的请求禁止跨域跳转")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_network(profile: dict, request: Request, *, session_headers: bool = False):
    """按 profile 打开 request；session_headers 为真时禁止跨域跳转；返回需由调用方关闭的响应。"""
    handlers = [] if profile["network"]["use_environment_proxy"] else [ProxyHandler({})]
    if session_headers:
        handlers.append(SameHostRedirect())
    return build_opener(*handlers).open(request, timeout=profile["network"]["timeout_seconds"])
