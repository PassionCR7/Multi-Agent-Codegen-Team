"""LangGraph 多智能体代码生成架构。

对外暴露的核心组件：
    build_graph()   -> 编译好的 LangGraph 图
    Workspace       -> 本地共享工作区
    ProjectScanner  -> 真实项目扫描器
"""

__version__ = "1.0.0"
__all__ = ["build_graph", "Workspace", "ProjectScanner"]


def __getattr__(name: str):
    # 延迟导入，避免仅导入子模块时触发重量级依赖（langgraph 等）
    if name == "build_graph":
        from .graph import build_graph

        return build_graph
    if name == "Workspace":
        from .workspace import Workspace

        return Workspace
    if name == "ProjectScanner":
        from .scanner import ProjectScanner

        return ProjectScanner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")