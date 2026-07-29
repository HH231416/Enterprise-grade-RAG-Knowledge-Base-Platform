"""对已部署的校园知识库检索接口做可复现的延迟压测。

输入文件为 JSONL，每行至少包含 {"query": "..."}，可选 filters、strategy、top_k。
该脚本只测检索接口，不把大模型生成耗时混入检索 P95；完整 QA 应另行压测。
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _request(url: str, payload: dict, api_key: str | None) -> tuple[float, int, int | None, str | None]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    started = time.perf_counter()
    try:
        with urlopen(Request(url, data=body, headers=headers, method="POST"), timeout=30) as response:
            status = response.status
            data = json.loads(response.read().decode("utf-8"))
        elapsed = (time.perf_counter() - started) * 1000
        service_latency = data.get("data", {}).get("latency_ms")
        return elapsed, status, int(service_latency) if service_latency is not None else None, None
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return (time.perf_counter() - started) * 1000, 0, None, str(exc)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="校园知识库检索延迟压测")
    parser.add_argument("queries", type=Path, help="JSONL 查询集")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080/api/v1")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--strategy", choices=("dense", "hybrid"), default="hybrid")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()
    if args.concurrency < 1 or args.repeat < 1:
        parser.error("concurrency 和 repeat 必须大于 0")

    rows = [json.loads(line) for line in args.queries.read_text(encoding="utf-8").splitlines() if line.strip()]
    payloads = []
    for _ in range(args.repeat):
        for row in rows:
            payloads.append({
                "query": row["query"],
                "top_k": int(row.get("top_k", args.top_k)),
                "strategy": row.get("strategy", args.strategy),
                "filters": row.get("filters"),
            })
    endpoint = f"{args.base_url.rstrip('/')}/retrieval/search"
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(_request, endpoint, payload, args.api_key) for payload in payloads]
        for future in as_completed(futures):
            results.append(future.result())

    elapsed = [item[0] for item in results if item[3] is None]
    service = [float(item[2]) for item in results if item[2] is not None and item[3] is None]
    errors = [item[3] for item in results if item[3] is not None]
    report = {
        "endpoint": endpoint,
        "requests": len(results),
        "concurrency": args.concurrency,
        "success": len(elapsed),
        "errors": len(errors),
        "client_latency_ms": {"p50": round(_percentile(elapsed, .50), 2), "p95": round(_percentile(elapsed, .95), 2), "p99": round(_percentile(elapsed, .99), 2)},
        "service_retrieval_latency_ms": {"p50": round(_percentile(service, .50), 2), "p95": round(_percentile(service, .95), 2), "p99": round(_percentile(service, .99), 2)},
        "mean_client_latency_ms": round(statistics.fmean(elapsed), 2) if elapsed else None,
        "sample_errors": errors[:5],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
