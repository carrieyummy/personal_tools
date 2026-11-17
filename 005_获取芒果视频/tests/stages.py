"""供隔离测试将同步下载和处理替身组合成正式阶段生成器。"""


def staged(download, process):
    """组合 download(job) 和 process(payload)，返回让出下载/处理边界并最终返回状态的任务工厂。"""
    def steps(job):
        """为 job 让出下载名额请求，下载后排队并返回处理终态。"""
        yield "download", None
        value = download(job)
        if isinstance(value, str):
            return value
        yield "ready", value
        return process(value)
    return steps
