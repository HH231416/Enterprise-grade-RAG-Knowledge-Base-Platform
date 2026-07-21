"""面试演示用脚本：运行混合检索基准测试并以 JSON 格式输出结果。

用法：python scripts/interview_metrics_benchmark.py
输出：12 个查询用例的 Hit@1 和 MRR 对比（稠密 vs 混合）
"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

import json   # JSON 序列化输出
import sys    # 系统路径操作
from pathlib import Path  # 路径处理


def main() -> int:
    """主函数：将项目根目录加入 sys.path，运行基准测试并输出 JSON 结果。"""
    # 获取项目根目录（scripts/ 的父目录）
    project_root = Path(__file__).resolve().parents[1]
    # 确保项目根目录在 Python 搜索路径中
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # 延迟导入基准测试模块
    from app.retrieval.controlled_benchmark import run_controlled_hybrid_benchmark

    # 执行基准测试并以 JSON 格式输出（ensure_ascii=False 保留中文）
    print(json.dumps(run_controlled_hybrid_benchmark(), ensure_ascii=False, indent=2))
    return 0  # 正常退出


if __name__ == "__main__":
    raise SystemExit(main())  # 以退出码 0 正常退出
