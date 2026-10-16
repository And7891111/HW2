"""Agent 抽象基类：所有 Workflow 节点必须实现的接口。

这是"可扩展性"维度的关键——新增一个 Agent 只需继承本类并实现 run()，
然后在 Workflow 引擎中注册即可，无需改动引擎。
"""

from abc import ABC, abstractmethod
from .context import WorkflowContext


class WorkflowNode(ABC):
    """统一的工作流节点接口，Agent 与流程控制节点都可由引擎调度。"""

    name: str

    @abstractmethod
    def run(self, context: WorkflowContext) -> None:
        """执行节点，并可读写共享状态。"""
        raise NotImplementedError


class Agent(WorkflowNode):
    """Workflow 节点抽象基类。

    每个 Agent 通过 run(context) 工作：
    1. 从 context 读取前一个 Agent 产出的状态
    2. 执行自己的职责
    3. 把结果写回 context，供下一个 Agent 使用
    """

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"<Agent:{self.name}>"
