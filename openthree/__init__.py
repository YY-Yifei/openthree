"""openthree —— 三生万物。

道生一，一生二，二生三，三生万物。

    道   = 模型本身（唯一的智能来源）
    一   = 系统提示词（.openthree/prompts/system-prompts.md）
    二   = functions（.openthree/functions/*.py，行动的能力）
    三   = ReAct 循环（观 → 思 → 行 → 观 …）
    万物 = 模型自己写出的新 function、新提示词、新工具、新世界

写 function 只需要两件事：类型注解 + docstring。

    from openthree import tool

    @tool
    def add(a: int, b: int) -> int:
        \"\"\"把两个数相加。

        Args:
            a: 第一个数
            b: 第二个数
        \"\"\"
        return a + b
"""

from .kernel import KERNEL, Kernel
from .tool import Tool, tool

__version__ = "0.1.0"

__all__ = ["tool", "Tool", "KERNEL", "Kernel", "__version__"]
