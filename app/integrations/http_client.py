# 允许在类型提示中使用尚未完全定义的类型。
from __future__ import annotations

# 从 collections.abc 导入 Callable，用于类型提示，表示一个参数是可调用对象（如函数）。
from collections.abc import Callable
# 从 time 模块导入 sleep 函数，用于在重试之间添加延迟。
from time import sleep

# 导入 httpx 库，一个现代化的、支持同步和异步的 HTTP 客户端。
import httpx

# 从项目核心模块导入配置和自定义错误类。
from app.core.config import Settings
from app.core.errors import AppError


# 定义一个函数，用于以 POST 方式发送 JSON 数据，并包含自动重试逻辑。
def post_json_with_retries(
    # `*` 表示所有后续参数都必须是关键字参数，这可以提高代码的可读性和健壮性。
    *,
    # 应用的配置实例
    settings: Settings,
    # 目标 URL
    url: str,
    # HTTP 请求头
    headers: dict,
    # 要作为 JSON 发送的 payload
    payload: dict,
    # 可选的单次请求超时时间（秒）
    timeout: float | None = None,
    # 一个“错误工厂”函数。它接收一个原始异常，并返回一个自定义的 AppError。
    # 这是一个非常优雅的设计，将通用的重试逻辑与特定的错误处理解耦。
    error_factory: Callable[[Exception], AppError],
) -> dict:
    # 从配置中获取重试次数，总尝试次数为 retries + 1。
    retries = settings.provider_retry_count + 1
    # 用于保存最后一次发生的异常。
    last_error: Exception | None = None
    # 确定请求超时时间：优先使用函数调用时传入的 timeout，否则使用配置中的默认值。
    request_timeout = timeout or settings.provider_timeout_seconds

    # 开始循环尝试发送请求。
    for attempt in range(retries):
        try:
            # 使用 httpx 发送 POST 请求。
            # json=payload 会自动将字典序列化为 JSON 字符串，并设置 Content-Type: application/json。
            response = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=request_timeout,
            )
            # 检查响应状态码。如果是 4xx 或 5xx，则会抛出 httpx.HTTPStatusError 异常。
            response.raise_for_status()
            # 如果请求成功，解析响应的 JSON 数据并返回，函数执行结束。
            return response.json()
        except Exception as exc:  # 捕获所有可能的异常，如连接错误、超时、HTTP 状态错误等。
            # 保存当前异常，以便在所有重试都失败后使用。
            last_error = exc
            # 如果这已经是最后一次尝试，则跳出循环，不再等待。
            if attempt >= retries - 1:
                break
            # 重试退避策略：等待一段时间再进行下一次尝试。
            # 等待时间线性增长（0.2s, 0.4s, ...），但最长不超过 1.0s。
            # 这可以防止在服务出问题时过于频繁地重试，给服务恢复的时间。
            sleep(min(0.2 * (attempt + 1), 1.0))

    # 如果循环正常结束（即所有尝试都失败了），那么 last_error 一定被赋值了。
    # assert 语句可以向静态分析器和开发者明确这一点。
    assert last_error is not None
    # 所有重试都失败后，调用错误工厂函数，将最底层的异常包装成业务定义的 AppError，并抛出。
    raise error_factory(last_error)