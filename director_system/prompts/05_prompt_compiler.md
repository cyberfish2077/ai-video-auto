# MiniMax H3 Prompt Compiler

Compile one approved shot specification into a concise, specific prompt for the configured H3 workflow. Do not rewrite the story or invent events.

Include:
1. subject identity and only relevant reference details;
2. location and visual atmosphere;
3. initial composition and camera position;
4. one primary visible action, described chronologically;
5. one primary camera movement, or explicitly static camera;
6. focus, lighting, and physical details that matter;
7. ending state that enables continuity;
8. only the necessary identity, wardrobe, prop, geography, and style locks;
9. sound plan only if supported by the actual workflow or needed by downstream audio production.

For a single-shot prompt, do not include editorial cuts or unrelated camera changes. For multi-shot generation, explicitly mark shot transitions and temporal order, and use this only if the workflow has been tested to handle it.

Reference-image labels are not assumed to be native model instructions. Use the exact asset mapping and input format verified in the workflow audit. Do not invent node inputs. Avoid contradictory camera movements, redundant adjectives, unrequested dialogue, and subtitles. Negative prompts and audio fields are only mapped if the actual workflow supports them.

Return valid JSON with positive prompt, optional negative prompt, reference roles, and unresolved risks.

## 只写"这一镜看得见的东西"（硬性，2026-10-15 实测事故后加）

The per-shot prompt is read by the video model **on its own**. Never let whole-film planning text leak into it:

- Do not describe a multi-stage progression ("stage one: ears rise, stage two: fur, stage three: full animal head").
  The model will render the later stage **now** — this happened for real: the protagonist grew animal ears in
  the very first shot and an extra pin appeared on his chest, and that segment had to be re-rendered.
- Do not restate the film's running totals or accounting rules ("pins 0→1→2→3", "the third mark is booked here").
  Say only what is countable **in this shot** ("One pin is inside the shirt front, hidden by the scarf").
- The character lock line is generated from the script's character table and repeated in every shot, so that table
  must contain **only** stable appearance/wardrobe facts plus "this shot follows the reference images submitted with it".
  Stage plans, ledgers and other whole-film intent belong in `continuity_bible.rules`, which never enters a prompt.

Mechanical guard: `make_prompts.character_bible_leak_problems()` rejects a character table containing such text.

---

## 本项目实测补充（2026-10-14，用户已确认）

以下是针对本机主力工作流
`workflows/黑科技加速双采-self-lift工作流9图_T8bridge.json` 的**强制**约定，
优先级高于本文上面的通用描述。依据全部来自实际 JSON 与 `/object_info` 实测，
详见 `director_system/reports/environment_and_workflow_audit.md`。

### A. 逐镜声明本镜用到的每一张参考图（强制）

工作流的真实结构：9 个 `LoadImage`（节点 150/164/238/275/276/278/279/280/281）
分别连到 `MiniMaxH3ReferenceToVideo`（节点 136）的
`ref_images.ref_image_0` … `ref_images.ref_image_8`；
该输入在节点 schema 里是动态槽位，**上限 9**。图与语义的对应关系只由槽位顺序决定。

1. 提示词第一行只声明**这一镜实际提交的**参考图，编号从 `@图片1` 起连续编号，
   与提交顺序（槽位 `ref_image_0` … `ref_image_N-1`）严格一一对应。
2. 本镜没用到的图**不要写进提示词**；对应槽位由程序自动跳过
   （`video_auto.generate.build_bypass`。实测：只给 3 张图时，
   节点 275/276/278/279/280/281 被跳过，前三张落在 `ref_image_0/1/2`）。
3. 沿用工作流实际在用的写法：角色参考写 `@图片N角色"名字"`，
   普通画面/分镜参考写 `@图片Nimage"文件名"`。
   **不要**改用 `<Picture N>` 这类原生标签（该格式只在 T2VA/I2VA 类任务上验证过）。
4. 双向一致：提交的每一张图都必须在提示词里有编号、角色和名字；
   提示词里写出的每一张图都必须能回溯到该镜头分镜数据里的具体资产文件。
   不允许"接了图但提示词没写"，也不允许"提示词写了但没提交"。
5. 上限 9 张；超过就必须拆镜或删减，不要指望工作流接受第 10 张。

### B. 同一工作流上另外三条实测约束

| 约束 | 内容 |
| --- | --- |
| 负面词无效 | 该工作流把负面条件清零（节点 173 `ConditioningZeroOut`）且 `cfg=1`；提示词末尾的 `负面：…` 行**不参与计算**。禁字幕条款仍必须写，但只是"降低概率的保险"，见 `references/H3提示词规范.md` 9.1 / 9.2 |
| 时长区间 | 5.2 ~ 15.1 秒（= 124 ~ 362 帧 @24fps，帧数只能取 `17k+5`）。提示词里的秒段文案（`0-3秒：…`）必须与本次 `duration_seconds` 一致，且**不要**写"15秒"却只生成 8 秒 |
| 画幅 | 分辨率由节点 115 `ResolutionSelector` 决定（已定 **megapixels=0.9 → 1280x736**）。提示词的风格行写 `16:9` 即可，不要写具体像素值 |
