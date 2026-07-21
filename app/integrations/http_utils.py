from __future__ import annotations


def build_openai_compatible_url(base_url: str, endpoint: str) -> str:
    """
    健壮地将基础 URL 和端点拼接成一个与 OpenAI API 兼容的完整 URL。

    这个函数会处理 base_url 末尾和 endpoint 开头的斜杠，确保它们之间
    总是有且只有一个斜杠。

    Args:
        base_url: 基础 URL，例如 "http://localhost:8080/v1"
        endpoint: API 端点，例如 "/embeddings" 或 "embeddings"

    Returns:
        一个格式正确的完整 URL 字符串。
    """
    # 1. 移除 base_url 末尾可能存在的所有斜杠，进行标准化处理。
    #    例如 "http://localhost:8080/" 会变成 "http://localhost:8080"。
    normalized_base = base_url.rstrip("/")

    # 2. 处理一个边缘情况：如果用户提供的 base_url 已经包含了完整的端点路径。
    #    例如 base_url = "http://myapi.com/v1/embeddings", endpoint = "/v1/embeddings"
    #    在这种情况下，直接返回标准化的 base_url 即可，无需重复拼接。
    if normalized_base.endswith(endpoint.rstrip("/")):
        return normalized_base

    # 3. 移除 endpoint 开头可能存在的斜杠。
    #    例如 "/embeddings" 会变成 "embeddings"。
    #    结合前面的 rstrip("/")，这确保了 base_url 和 endpoint 之间永远有且只有一个斜杠。
    #    最后使用 f-string 将标准化的基础 URL 和端点拼接起来。
    return f"{normalized_base}/{endpoint.lstrip('/')}"