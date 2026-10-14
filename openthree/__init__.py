"""openthree —— 一个会给自己起名字的智能体内核。

四层结构（「道生一，一生二，二生三，三生万物」）：

    道   = 模型本身（唯一的智能来源）
    一   = 系统提示词（.openthree/prompts/system-prompts.md，柔性骨架，模型可自改）
    二   = functions（.openthree/functions/*.py，行动的能力）
    三   = ReAct 循环（观 → 思 → 行 → 观 …）
           + 计划（.openthree/prompts/plan.md，长任务待办，每一步自动注入）
    万物 = 模型自己写出的新 function、新提示词、新工具、新名字、新世界

名字不是内核常量：智能体用 `set_identity` 给自己起名（见 .openthree/identity.json），
内核只在它还没有名字时兜底显示 "openthree（未命名）"。

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
from .multimodal import (ImageError, ImageResult, content_text, image_result,
                         to_data_url)
from .tool import Tool, tool

__version__ = "0.1.1"

__all__ = [
    "tool", "Tool", "KERNEL", "Kernel",
    # 多模态：工具返回图片用 ImageResult
    "ImageResult", "ImageError", "image_result", "to_data_url", "content_text",
    "__version__",
]
