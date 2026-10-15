---
name: ai-video-auto
description: 把中文讲稿自动做成 2 分钟以上的 AI 长视频：DeepSeek 拆段写提示词 → 本地 ComfyUI 的 MiniMax-H3 多参考图工作流出片 → FFmpeg 合成 → 字幕体检与中文制作报告；含任务台账、断点续跑、受控重生成。当用户要在这台机器上做短片/成片、或排查这套流水线的问题时使用。没有本地 ComfyUI 与 H3 模型的环境不适用。
metadata:
  short-description: 讲稿到成片的全自动长视频流水线
---

# AI Video Auto（讲稿 → 长视频）

这个仓库既是要用的工具，也是本技能本体。**所有命令都在仓库根目录（下称 `<项目根>`）执行**，
运行脚本一律用项目自己的虚拟环境解释器（Windows：`.venv\Scripts\python.exe`）——
不要用系统 `py`，更不要用 ComfyUI 自带的 `python_embeded`。

## 三条路线：按用户怎么开口来选

| 用户说的话 | 走哪条 | 人工确认 |
| --- | --- | --- |
| 只给主题（「我想做一个关于…的视频，2 分钟，写实风格」） | **快速线**：`scripts\make_video.py`（讲稿 → 分段 → 参考图 → 出片 → 合成 → 字幕体检 → 报告） | **零**：只在缺关键输入、或画面有硬伤需要决定时找他 |
| 出现**「快速出片」**或**「无审核」** | **快速出片**：`director_system\scripts\make_film_fast.py`（主题 → 剧本 → 分镜 → 逐镜六段式提示词 → 渲染计划 → 参考图 → 出片 → 合成） | **零**（同上） |
| 出现**「导演系统」** | **严格审核导演系统**：`make_ideas.py → make_story.py → make_storyboard.py → make_prompts.py`（每步都过独立评审） | **3 道闸门** A 选案 / B 制作前确认 / C 成片质检，每道停下等人批准 |

三条路线共用同一套执行层（ComfyUI 出图出片 + FFmpeg 合成 + 交付前核对）。
完整命令表与各自的产物位置见 `references/入口指引-三条路线怎么选.md`。
忘了做到哪一步：严格版敲 `director_system\scripts\next_step.py`；
批量出片敲 `scripts\run_batch.py status --project <项目名>`。

## 开工前的三条只读检查

1. `<项目根>\.venv\Scripts\python.exe scripts\check_env.py` —— 应显示"0 错误"。
   它同时会检查 ComfyUI 是否在线、队列是否空闲；不通过就别开工，报错最后一行就是修法。
2. 读 `config/settings.yaml`（不存在就从 `settings.example.yaml` 复制后改路径）。
   代码里没有硬编码路径，路径**全部**来自这个文件。
3. 确认讲稿在 `data\briefs\<名字>.txt`。没有就先帮用户写一条（参考 `data\briefs\示例讲稿.txt`）。

## 做一条片（首选：一条命令）

```powershell
.venv\Scripts\python.exe scripts\make_video.py --name <项目名> --brief-file data\briefs\<讲稿>.txt --minutes 2 --style photoreal-cinematic
```

步骤：`script`（DeepSeek 写剧本与提示词）→ `ledger`（建台账）→ `materials`（出角色三视图与分镜图）
→ `sync`（同步进 ComfyUI 的 input）→ `segments`（H3 逐段出片）→ `compose`（FFmpeg 合成）
→ `check`（字幕体检）→ `report`（中文制作报告）。

* 加 `--plan` 只打印计划不执行；`--steps` / `--skip` 只跑或跳过某几步。
* **命令可反复执行**：已完成的步骤自动跳过，中断后重跑就是续跑，不会重复消耗显卡。
* **时间预算**：每 15 秒段落约 8 分钟（2 分钟成片 ≈ 70 分钟）。长任务请后台跑并定期看
  `data\logs\` 的日志，不要阻塞等待。
* 风格由 `config/styles.yaml` 的档位决定（`guofeng-cg` 国风 3D / `photoreal-cinematic` 电影写实）。
  做现代、科幻、灾难、写实题材**必须**改用写实档，否则会被国风的风格词带偏。

## 红线（违反即视为错误）

1. **不碰 ComfyUI 侧**：不重装或升级 ComfyUI、H3 模型、PyTorch、CUDA、custom_nodes，
   不往它的解释器里 `pip install`。项目只通过 `http://127.0.0.1:8188` 的 API 使用它。
2. **不猜 H3 参数**：节点 id、输入名、输出路径必须来自实际工作流 JSON 与
   `http://127.0.0.1:8188/object_info`；工作流改动后要重跑 `scripts\inspect_workflow.py` 核对。
3. **密钥只从 `.env` / 环境变量读**，绝不写进源码、日志、报告、Git 提交；
   必须打印时用 `video_auto.config.mask_secret` 掩码。
4. **不覆盖、不删除用户产物**：合成默认不覆盖已有成片（要覆盖必须显式 `--force`）；
   重做某段用 `run_batch.py regen`（它先备份台账、再把旧产物移进 `segments\_superseded\`），
   **不要手工删 `segments.json`**。
5. 每个阶段的结论都要落盘（`projects\<项目名>\` 下的 `script.md`、`prompts/`、`segments.json`、
   `compose/`、`analysis/`），不要只留在对话里。

## 改台词或改画面：先改事实来源，再重跑

`projects\<项目名>\prompts\seg-0N.txt` 是**运行时的事实来源**（台账里那份只是快照）。
改完用
`run_batch.py regen --project <项目名> --id seg-0N --reason "..." --seed <换一个新种子>`
重做该段，再 `compose_final.py --project <项目名> --force` 重新合成，最后重跑体检与报告。
重新合成前，先把现有成片移到 `final\_superseded\` 留档。

**不要删提示词里的【人物锁定】行**：它由角色设定自动生成，是防止"主角被画成路人、
外套被换色"的关键（见 `references/实测经验与坑.md`）。

## 说"完成"之前的验收

* 成片：`ffprobe` 看时长与规格；`ffmpeg -v error -i <成片> -f null -` 完整解码无报错。
* 画面：抽帧只看**降采样拼图**（宽 ≤ 768px、单张 ≤ 200KB），
  **单个对话累计读图 ≤ 10 张**，结论立刻写进 `projects\<项目名>\analysis\*.md`。
  实测教训：图片塞满历史会让请求体超限（`413 Payload Too Large`），之后该对话**任何指令**
  都发不出去，只能另开对话。
* 字幕：`scripts\check_segment_text.py --project <项目名> --write-report`（只报告，不改画面）。
* 报告：`scripts\project_report.py --project <项目名>`。

## 出问题去哪查

| 现象 | 先看 |
| --- | --- |
| 环境 / ComfyUI / 密钥不通 | 在 `<项目根>` 跑 `check_env.py`，照它最后一行修 |
| 不知道接着跑什么、某段失败 | `run_batch.py status --project <项目名>`、`data\logs\batch-*.log` |
| 画面风格不对（冒出动漫感或古风） | `config/styles.yaml` 的档位；换档后重做该段 |
| 主角长相或服装跨段漂移 | `references/实测经验与坑.md`（人物锁定 + 连续性锚点） |
| 画面里出现字幕 | `references/实测经验与坑.md`（H3 已知行为，去不掉，只能接受或后处理） |
| 第一次接入、或要换工作流 | `references/工作流接入说明.md` |

## references（按需读，不要一次全读）

* `references/安装与前置条件.md` —— 新机器从零跑起来要准备什么（Python / ffmpeg / ComfyUI / 模型 / 密钥）。
* `references/使用手册.md` —— 日常操作一页纸（做片、续跑、重做、体检、常见现象对照表）。
* `references/入口指引-三条路线怎么选.md` —— 三条路线各自的入口命令、产物、怎么确认走对了。
* `references/工作流接入说明.md` —— 怎么把**你自己的** ComfyUI 工作流接到本项目上。
* `references/H3提示词规范.md` —— 提示词模板怎么写（含禁字幕条款与实测更正）。
* `references/实测经验与坑.md` —— 时间基准、已知坑、验收怎么量化。
