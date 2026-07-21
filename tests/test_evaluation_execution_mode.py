"""测试评测执行模式：验证 background 模式正确入队而不阻塞请求。"""

# 启用 PEP 563 延迟注解求值，避免循环引用并提升模块导入速度
from __future__ import annotations

from app.core.config import get_settings, reset_settings_cache  # 配置和缓存重置
from app.schemas.evaluation import DatasetCreateRequest, QueryCreateItem, RunCreateRequest  # 评测 Schema
from app.services.evaluation import EvaluationService  # 评测服务


def test_background_evaluation_mode_enqueues_without_blocking(db_session, monkeypatch):
    """验证 background 模式下创建评测运行时不阻塞，任务被正确入队。

    流程：
    1. 设置 EVALUATION_EXECUTION_MODE=background
    2. 创建评测数据集
    3. 用 monkeypatch 拦截后台任务提交
    4. 创建评测运行，断言状态为 pending 且任务已入队
    """
    # 设置环境变量为 background 模式
    monkeypatch.setenv("EVALUATION_EXECUTION_MODE", "background")
    reset_settings_cache()  # 清除配置缓存，使新环境变量生效
    service = EvaluationService(db_session)
    # 创建评测数据集（含一个查询）
    dataset = service.create_dataset(
        DatasetCreateRequest(
            name="background-evaluation",
            queries=[QueryCreateItem(query_text="transfer approval")],
        )
    )
    submitted = {}  # 用于记录拦截到的提交参数

    def fake_submit(*, run_uuid, doc_uuids):
        """伪造的提交函数：记录参数而非实际提交。"""
        submitted["run_uuid"] = str(run_uuid)
        submitted["doc_uuids"] = doc_uuids

    # 用 monkeypatch 替换后台任务提交方法
    monkeypatch.setattr(
        "app.services.background_jobs.BackgroundJobRunner.submit_evaluation_run",
        fake_submit,
    )

    # 创建评测运行
    run = service.create_run(RunCreateRequest(dataset_uuid=dataset.dataset_uuid))

    # 断言：状态为 pending（未被阻塞执行）
    assert run.status == "pending"
    # 断言：任务参数正确入队
    assert submitted == {"run_uuid": run.run_uuid, "doc_uuids": []}
    reset_settings_cache()  # 恢复配置缓存
