---
name: local-img2img
description: |
  英特尔本地图生图（本地图像编辑），仅限 Intel AIPC 平台。根据源图片和提示词在本地修改、编辑、重设风格、转换或重新生成图片。
  触发词：修改图片 / 编辑图片 / 图生图 / 换背景 / 去水印 / 把这张图改成……，以及 edit / modify / transform / restyle this image。
  当用户提供图片路径并要求在这台 Intel AIPC 上编辑图片时，优先使用此本地技能，而不是在线图像编辑服务。
allowed-tools: Bash
disable-model-invocation: false
user-invocable: false
---

# 本地图生图（Local-Img2Img）

在 Intel AIPC 上本地执行图生图：给定源图片 + 编辑提示词，输出一张修改后的图片。推理完全在本地完成，不上传云端。

## 调用方式

通过 Bash 工具调用本技能 scripts 目录下的入口脚本：

```
scripts/run.ps1 "<image-path>" "<prompt>"
```

- 仅两个参数：源图片绝对路径、编辑提示词。
- 在 Windows 的 Bash 环境中，若直接调用失败，可用 `powershell -ExecutionPolicy Bypass -File scripts/run.ps1 "<image-path>" "<prompt>"` 执行。
- 首次调用会自动下载 FLUX.2-klein OpenVINO 模型并构建 Python 环境；**仅第一次**需要完整安装。若上次下载停在 `.partial` 且**权重 `.bin` 已齐**，会自动晋级为正式模型目录；若只有 `model_index.json` 等小文件而缺少权重，会判定为不完整并重新下载，不会误当成「依赖安装失败」。
- 本地联调可直接复制 `templates/` 下四条提示词（见 `templates/README.md`），无需 WorkBuddy 胶囊/工作模式。
- 输出为源图同目录下的 `<原文件名>_edited_<时间戳>.png`，不覆盖原图。

示例：

| 意图 | 命令 |
| --- | --- |
| 更换主体 | `scripts/run.ps1 ".\dog.png" "replace the dog with a cat wearing a tiny straw hat"` |
| 更换背景 | `scripts/run.ps1 ".\portrait.jpg" "make the background a sunny beach"` |
| 重设风格 | `scripts/run.ps1 ".\room.png" "turn this into a warm watercolor illustration"` |
| 中文提示词 | `scripts/run.ps1 ".\input.png" "把背景改成雨夜霓虹街道，保留主体姿势"` |

## 模型下载与续传

- 若首次运行因下载模型而超时，客户端会以如下信息退出：

  ```
  模型正在下载, 请用命令`scripts\run.ps1 --continue`继续运行
  ```

  此时重复执行 `scripts/run.ps1 --continue`，直到出现正常结果。
- 下载过程中 stdout 里以 `模型下载中` 开头的行需如实转达给用户：首行立即展示，之后约每 5 分钟刷新一次（百分比、已下载/总量、速度、ETA）。

## 结果解读

每次成功编辑会打印：

- `图片已修改: <绝对路径>` — 修改后的图片文件
- `原图` — 源图片路径
- `提示词`、`种子`、`参数`、`设备` — 本次输入
- `耗时` — 加载 / 推理 / 保存耗时

常见错误码：

- `BAD_IMAGE` — 源图片缺失、不可读或非法
- `BAD_PROMPT` — 提示词为空或非字符串
- `GENERATION_FAILED` — OpenVINO 推理阶段异常
- `SAVE_FAILED` — 输出 PNG 无法写入

## 运行前提与边界

- 仅支持 Intel AIPC 平台（Windows）。在非 AIPC 上会提示 `This skill requires an Intel AIPC platform` 并退出。
- 首次使用需联网安装 Python 依赖与下载模型。
- 非在线图像生成代理，全部推理本地完成。
- 不提供 inpainting mask、ControlNet、LoRA 等精细控制（全图重绘，无局部遮罩）。
- 失败时不要静默回退到在线图像编辑服务，需如实告知用户失败原因。
