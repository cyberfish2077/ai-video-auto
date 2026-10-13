# AI Video Auto —— 讲稿到成片的全自动长视频流水线

给**本地 ComfyUI + MiniMax-H3**（多参考图生视频）环境用的一套自动化：你给一条中文讲稿，
它把整条片子做出来——DeepSeek 拆段写提示词 → 生成角色设定图与分镜图 → H3 逐段出片 →
FFmpeg 合成带交叉溶解的成片 → 字幕体检 → 中文制作报告。

一条命令做一条 2 分钟成片（实测 68 分钟，含出图与 8 段出片）：

```powershell
.venv\Scripts\python.exe scripts\make_video.py ^
    --name my-film-01 --brief-file data\briefs\我的故事.txt --minutes 2 ^
    --style photoreal-cinematic
```

它同时是一个 **Codex skill**：仓库根目录的 `SKILL.md` 让 Codex 能直接接管这套流水线。

## 它解决什么问题

长视频不是"把一条提示词拉长"能做的，真正麻烦的是工程问题：

* **分段与连续性**：2 分钟要拆成 8 段×15 秒，接缝不能跳脸、不能跳衣服颜色；
* **断点续跑**：一段要跑 8 分钟，中途断电/重启不能前功尽弃；
* **可追溯**：每段的提示词、种子、尝试次数、产物路径都要落盘，事后能查、能定点重做；
* **验收**：模型会偷偷把台词烧成画面字幕，得有一道自动体检；
* **成本可控**：改一段只重做一段，不重跑整条片子。

## 快速开始

```powershell
# 1) 装依赖（只装本项目的库，绝不装进 ComfyUI 的解释器）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 配置：复制模板后按自己机器改路径
copy config\settings.example.yaml config\settings.yaml
copy .env.example .env      # 填 DEEPSEEK_API_KEY

# 3) 自检（应显示 0 错误；它会顺带检查 ComfyUI 是否在线、队列是否空闲）
.venv\Scripts\python.exe scripts\check_env.py

# 4) 做一条片
.venv\Scripts\python.exe scripts\make_video.py --name my-film-01 ^
    --brief-file data\briefs\示例讲稿.txt --minutes 2 --style photoreal-cinematic
```

Windows 用户也可以直接双击 `开始制作.cmd`，按提示回答四个问题（项目名 / 讲稿 / 时长 / 风格档位），
它会先打印计划、等你确认再开跑。详细步骤见 `references/安装与前置条件.md`。

## 目录结构

```text
├── SKILL.md              Codex 技能入口（也是这套流水线的操作守则）
├── config/
│   ├── settings.example.yaml   路径与后端配置模板（复制为 settings.yaml 后修改）
│   ├── styles.yaml             风格档位：风格词 / BGM 要求 / 负面词 / 给模型的要求
│   └── workflow_targets.yaml   工作流"参数改写点"登记表（换工作流改这里）
├── src/video_auto/       业务代码（config / comfy / deepseek / script_gen / workflow /
│                         generate / tasks / compose / textcheck / logging_util）
├── scripts/              命令行入口（每个脚本只做参数解析与调用）
│   ├── check_env.py            环境自检
│   ├── make_video.py           一条命令：讲稿 → 成片
│   ├── make_script.py          讲稿 → 剧本 + 每段提示词 + 素材任务
│   ├── run_batch.py            任务管理器：create / run / status / reset / regen
│   ├── inspect_workflow.py     工作流体检（节点与输入名核对）
│   ├── sync_inputs.py          素材同步进 ComfyUI input
│   ├── compose_final.py        FFmpeg 合成（交叉溶解 + 音频淡化，默认不覆盖成片）
│   ├── check_segment_text.py   字幕体检（只报告，不改画面）
│   └── project_report.py       中文制作报告
├── tests/                49 个单元测试（不联网、不占显卡）
├── references/           给 Codex 读的详细文档（安装 / 手册 / 工作流接入 / 提示词规范 / 实测经验）
├── workflows/           随包发布的两份工作流：出片（T8bridge 9 图多参考）+ 出图（Qwen-Image 2.1）
└── data/briefs/          讲稿放这里（示例讲稿 + 一条真实讲稿）
```

每个作品一个目录，文本入 Git、大文件留本地：

```text
projects/<项目名>/
├── script.md          剧本                ├── segments.json   任务台账（状态/尝试/种子/产物）
├── prompts/seg-0N.txt 每段提示词（运行时的事实来源，可手改后重跑）
├── project.json       生产定义            ├── compose/        合成记录
├── materials/         角色三视图与分镜图  ├── segments/       分段视频（含 _superseded/ 留档）
└── analysis/          体检结论与抽检记录  └── final/          成片（含 _superseded/ 留档）
```

## 设计取舍（为什么这么做）

* **只走 API，不进 ComfyUI 内部**：不装包、不改节点，避免污染那套环境；工作流升级时
  只需重跑 `inspect_workflow.py` 核对节点 id，改 `config/workflow_targets.yaml` 即可。
* **格式由代码拼，内容由模型写**：H3 的中文时间轴模板、禁字幕条款、跨段连续性、人物锁定
  全部由 `script_gen` 按固定模板生成，DeepSeek 只负责剧情、镜头、台词、音效——
  模型发挥再野，格式也不会跑偏。
* **台账是唯一事实来源**：`segments.json` 记录每段的状态/种子/尝试次数/产物路径；
  提示词文件优先于台账快照。所以"改提示词重跑"一定生效，"重做一段"一定只花一段的钱。
* **负面词不是开关**：本项目验证过——这套工作流把负条件清零（`ConditioningZeroOut`）、
  采样 `cfg=1.0`，写在正文里的"负面：字幕…"**没有约束力**（详见 `references/H3提示词规范.md`）。
  不要指望靠负面词解决画面问题。

## 已知限制（务必先读）

1. **强依赖本地环境**：Windows + 可用的 ComfyUI（含 MiniMax-H3 相关模型：扩散模型、LoRA、
   video/audio VAE、Qwen3-VL 文本编码器）+ 支持 CUDA 的 N 卡。本项目自带代码，**不含模型权重**。
2. **随包只带两份工作流**：`workflows/` 下有主力出片（黑科技加速双采 self-lift 9 图 T8bridge）
   与出图（Qwen-Image 2.1 文生图）两份，跑主流程够用。它们来自第三方 ComfyUI 整合包，
   **版权属于原作者**，此处按其原样附上以便开箱可用；如原作者有异议请移除。
   其余工作流（T2V / 首尾帧 FL2V / 音乐生成）请自备，接入方法见 `references/工作流接入说明.md`。
3. **台词可能被烧成画面字幕**：H3 的已知行为，换种子、改写法、去掉"字幕"字样都试过，去不掉。
   本项目提供体检工具定位它，并提供 `delogo` 定点修复的备用方案，但不保证消除。
4. **跨段造型会缓慢漂移**：靠"上一段末帧当参考图"传递连续性，偏差会逐段放大。
   已用【人物锁定】+ 逐镜点名把起点钉死、把漂移压小，但整片完全一致不保证。
5. **成本与时间**：每 15 秒约 8 分钟 GPU 时间；2 分钟成片约 70 分钟、一张 16GB 卡。
6. 生成内容由第三方模型产出，**发布前请自行确认素材与成片的合规性**。

## 测试

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -q     # 49 项，约 0.15 秒
```

## 许可

代码以 MIT 许可发布（见 `LICENSE`）。第三方模型、ComfyUI 及其整合包、工作流 JSON
各有其自身许可，不在本许可范围内。

> 开发过程中的完整进展记录（逐次实测数据、踩坑过程）保留在作者本地仓库，不随本包分发；
> 其中可复用的结论已整理进 `references/实测经验与坑.md`。
