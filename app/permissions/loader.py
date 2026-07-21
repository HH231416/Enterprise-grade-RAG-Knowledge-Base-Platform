# -*- coding: utf-8 -*-
import importlib
import logging
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 全局变量，用于缓存权限检查器的单例实例
# 使用 _ 开头表明它是一个内部变量，不建议外部直接访问
_checker_instance = None


def load_permission_checker() -> Any:
    """
    加载并返回权限检查器的实例。

    该函数是本项目中一个设计精巧的模块，综合运用了多种软件设计模式和Python高级特性，
    非常适合在面试中作为展示技术深度的案例。

    核心设计思想：
    1.  **单例模式 (Singleton Pattern)**:
        - 通过全局变量 `_checker_instance` 缓存已创建的实例。
        - 函数首次调用时创建实例，后续调用直接返回该实例，确保全局唯一。
        - 优点: 保证实例唯一性，避免重复创建开销，提供全局访问点。
        - 面试考点:
            - 单例模式的实现方式有哪些？(全局变量、装饰器、元类、__new__方法)
            - 这种实现在多线程环境下是否安全？(不安全，可能需要加锁 `threading.Lock` 来保证线程安全)。

    2.  **工厂模式 (Factory Pattern)**:
        - 根据配置 `settings.permission_mode` 的值 ('none' 或 'plugin') 来决定创建并返回哪种具体的权限检查器实例。
        - 将对象的创建逻辑与使用方解耦，使系统更灵活、可扩展。

    3.  **插件化设计与动态导入 (Plugin Architecture & Dynamic Import)**:
        - 当 `permission_mode` 为 'plugin' 时，系统具备了插件化的能力。
        - `importlib.import_module()`: 这是实现插件系统的核心。它允许程序在运行时根据字符串（模块路径）动态加载Python模块。
          这比静态的 `import` 语句灵活得多，是许多框架和大型应用实现扩展性的关键。
        - `getattr()`: Python的反射机制。在动态导入模块后，使用 `getattr` 根据类名的字符串从模块对象中获取类定义。
        - 面试考点:
            - 谈谈你对Python动态导入的理解和应用场景（如插件加载、配置驱动的模块选择等）。
            - `__import__` 和 `importlib.import_module` 有什么区别？(后者是推荐的、更高级的API)。

    4.  **异常链 (Exception Chaining)**:
        - `raise ValueError(...) from e`: 在捕获到底层异常 `e` 后，抛出一个更具体的业务异常 `ValueError`，同时保留原始异常的上下文。
        - 这样做可以提供更清晰的错误追溯信息，极大地帮助了问题定位。

    Returns:
        Any: 返回一个符合权限检查器接口的实例。
    """
    global _checker_instance

    # --- 单例模式实现 ---
    # 如果实例已存在，直接返回，避免重复创建
    if _checker_instance is not None:
        return _checker_instance

    settings = get_settings()

    # --- 工厂模式实现 ---
    # 根据配置决定创建哪种对象
    if settings.permission_mode == "none":
        # 模式为 'none'，加载一个不做任何操作的检查器
        # 延迟导入（lazy import）: 仅在需要时才导入，可以略微提高启动性能
        from app.permissions.noop import NoOpPermissionChecker
        _checker_instance = NoOpPermissionChecker()
        logger.info("Loaded NoOpPermissionChecker (permission_mode=none)")
    elif settings.permission_mode == "plugin":
        # 模式为 'plugin'，动态加载用户配置的插件
        plugin_path = settings.permission_plugin
        try:
            # --- 动态导入与反射 ---
            # 1. 将插件路径分割为模块路径和类名，例如 "a.b.C" -> "a.b", "C"
            module_path, class_name = plugin_path.rsplit(".", 1)
            # 2. 使用 importlib 动态导入模块
            module = importlib.import_module(module_path)
            # 3. 使用 getattr 从模块中获取类对象
            checker_class = getattr(module, class_name)
            # 4. 创建类的实例
            _checker_instance = checker_class()
            logger.info(f"Loaded permission checker: {plugin_path}")
        except Exception as e:
            # --- 异常链 ---
            # 动态加载失败，记录错误并抛出带有原始异常信息的更高级别异常
            logger.error(f"Failed to load permission checker {plugin_path}: {e}")
            raise ValueError(f"Failed to load permission checker: {plugin_path}") from e
    else:
        # 配置错误，抛出异常
        raise ValueError(f"Unknown permission_mode: {settings.permission_mode}")

    return _checker_instance


def reset_permission_checker() -> None:
    """Clear the cached checker after a configuration or plugin change."""
    global _checker_instance
    _checker_instance = None
