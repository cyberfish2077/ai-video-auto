"""AI 长视频自动化系统主程序包。

本包只负责“编排”：讲稿 → DeepSeek 拆段与提示词 → ComfyUI(MiniMax-H3) 出片段
→ FFmpeg 合成 → 报告。

约定：
* 所有路径来自 config/settings.yaml，不在业务代码中硬编码。
* 所有密钥来自 .env 或环境变量，绝不写入日志与提交。
"""

__version__ = "0.1.0"
