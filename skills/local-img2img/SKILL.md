---
name: local-img2img
description: |
  英特尔本地图生图（本地图像编辑），仅限 Intel AIPC 平台。根据源图片和提示词在本地修改、编辑、重设风格、转换或重新生成图片。
  触发词：修改图片 / 编辑图片 / 图生图 / 换背景 / 去水印 / 去杂物 / 商品场景 / 自然美颜 / 老照片修复 / 把这张图改成……，以及 edit / modify / transform / restore this image。
  当用户提供图片路径并要求在这台 Intel AIPC 上编辑图片时，优先使用此本地技能，而不是在线图像编辑服务。
description_zh: Intel AIPC 本地图生图与照片编辑技能
description_en: Local image-to-image photo editing for Intel AIPC
version: 1.1.9
author: Haoruo Xu
allowed-tools: Write, Bash, TaskOutput, TaskStop, Read
disable-model-invocation: false
user-invocable: false
---

# 本地图生图（Local-Img2Img）

在 Intel AIPC 上本地执行图生图：给定源图片 + 编辑提示词，输出一张修改后的图片。推理完全在本地完成，不上传云端。

## 调用方式

先生成一个 RFC 4122 UUID（下称 `<REQUEST_UUID>`），再用 `Write` 在当前 WorkBuddy 工作空间根目录创建 `.photo-magic-request-<REQUEST_UUID>.json`。用户输入只能出现在 JSON 文件中，不得拼进 shell 命令：

```
{
  "image_path": "<源图片绝对路径>",
  "prompt": "<编辑提示词>",
  "scene": "<七个场景 ID 之一>"
}
```

`scene` 只能是：`background-swap`、`mark-removal`、`id-photo-background`、`clutter-removal`、`product-scene`、`natural-portrait`、`old-photo-restoration`。随后只执行这一条标准命令：

```
bash "${CODEBUDDY_SKILL_DIR}/scripts/run.sh" --request-file ".photo-magic-request-<REQUEST_UUID>.json" "<REQUEST_UUID>"
```

- 命令中的两处 `<REQUEST_UUID>` 必须替换成写请求文件前生成的同一个真实 UUID；只使用上述 Bash 入口，不切换其他 shell 或脚本。标准命令明确失败即报告真实错误，不读取脚本猜测调用方式，不擅自重复启动。
- JSON 在客户端成功读取并校验后立即删除；模型在发现位置原地复用。宿主没有提供插件数据变量时，虚拟环境、日志、锁与临时文件稳定保存在 `~/.openvino/photo-magic`；成品只写当前工作空间的 `outputs/`。
- 命令暂时没有 stdout、仍处于运行状态或宿主返回后台任务 ID，都不算失败；继续等待同一个进程，绝不能再启动第二份。“已开始处理”只是进度消息，发送后必须立刻继续执行 Bash/TaskOutput，不能结束当前回复。
- **不要单独再调 `client.py` / `server.py`**：`run.sh` 会在环境就绪后自动启动推理客户端；若模型仍在下载（退出码 3），会在总时限内自动 `--continue`，直到出图或明确失败。
- 每次新请求会打印唯一 `Request ID`，并用它隔离并发会话。手动续传时优先使用 `bash scripts/run.sh --continue <request-id>`；不指定 ID 只允许系统中恰好存在一个待处理请求。
- 首次调用依次检查显式指定的 `LOCAL_IMG2IMG_MODEL_DIR`、`~/.openvino/models/FLUX.2-klein-4B-int4-ov` 和旧版 `~/.openvino/photo-magic/models/FLUX.2-klein-4B-int4-ov`。发现目录就原地复用，不复制、不迁移、不重新下载；都不存在时才下载到共享目录。若上次下载停在目标目录旁的 `.partial` 且**权重 `.bin` 已齐**，会自动晋级为正式模型目录。
- 本地联调可直接复制 `templates/` 下七条提示词（见 `templates/README.md`），无需 WorkBuddy 胶囊/工作模式。
- 输出固定写入当前 WorkBuddy 工作空间根目录下的 `outputs/`，文件名为 `<原文件名>_edited_<时间戳>_<短ID>.png`，不依赖 Bash 当前目录，不覆盖原图，也不把成品写入插件数据目录。

示例：

| 意图 | `scene` | 模板 |
| --- | --- | --- |
| 更换背景 | `background-swap` | `templates/01-swap-background.txt` |
| 去标记 | `mark-removal` | `templates/02-remove-watermark.txt` |
| 证件照换底 | `id-photo-background` | `templates/03-id-photo-recolor.txt` |
| 去杂物 | `clutter-removal` | `templates/04-remove-clutter.txt` |
| 商品场景 | `product-scene` | `templates/05-product-scene.txt` |
| 自然人像美颜 | `natural-portrait` | `templates/06-natural-portrait.txt` |
| 老照片修复 | `old-photo-restoration` | `templates/07-old-photo-restoration.txt` |

## 模型下载与续传

- 下载未完成时，`client.py` 可能暂时退出码 3。**优先继续等同一个 `run.sh` 进程自动续跑**；仅在你手动中断后，才需要：

  ```
  bash "${CODEBUDDY_SKILL_DIR}/scripts/run.sh" --continue <request-id>
  ```

- 下载过程中 stdout 里以 `模型下载中` 开头的行需如实转达给用户：首行立即展示，之后约每 5 分钟刷新一次（百分比、已下载/总量、速度、ETA）。
- 单次入口总运行时间最多 60 分钟；模型下载或初始化连续 15 分钟没有可观察进展时失败并停止服务。已下载的 `.partial` 文件保留，之后可续传。
- 冷启动的 `server-dog start` 握手单独等待最多 90 秒，普通 IPC 仍为 10 秒；模型导入或首次建管道超过 10 秒不会再被误判为启动失败。若 OpenCL 报 `no opencl gpu device available`，只做一次延迟重启，仍失败即报告 Intel GPU/OpenCL 驱动不可用，不循环重启，也不静默切换 CPU 或在线服务。
- WorkBuddy 下由 server-dog 直接启动 uv 环境对应的真实 CPython，避免 `pythonw.exe` 跳板二次创建进程被沙箱拒绝。每次启动使用独立 boot log，旧的 oneDNN/OpenCL 探测信息不会再被误报为本次死因；服务关闭时主动唤醒命名管道监听线程，优雅释放 GPU。
- 阶段日志顺序：`[1/3] Environment check` → `[2/3] Start inference client` → `正在启动推理服务` / `等待模型就绪` / `开始生成` → `[3/3] Inference finished`。看到环境就绪**不等于**任务结束。

## 后台任务等待规则

标准命令属于长任务，调用 Bash 工具时必须设置 `run_in_background=true`，让 WorkBuddy 返回唯一后台任务 ID。不要在命令末尾添加 `&`、输出重定向或自行启动第二层后台进程。

- 取得任务 ID 后立即调用 `TaskOutput`；每次使用 `block=true, timeout=60000`，最长等待 60 秒。任务提前完成时应立即返回，不需要等满 60 秒。
- `TaskOutput` 等待超时不代表图片生成失败。后台任务仍在运行且仍有进展时，继续使用 60 秒窗口轮询，不要重新启动 `run.sh` 或 `client.py`。
- 一旦输出出现 `图片已修改: <绝对路径>`，立即停止轮询，使用 `Read` 读取这个精确路径，并把图片本身作为产物返回给用户；不能只回复文件路径或声称“无法读取”。
- 只有后台任务明确以非零退出码结束，或宿主明确报告任务已失败、被取消或被终止时，才报告失败。
- 不要使用单次 `timeout=300000` 的长时间阻塞等待；分段等待期间应及时转达新的模型下载或生成阶段信息。
- 记录开始时间和最后一次有效进度：总等待达到 60 分钟，或连续 15 分钟没有下载字节/阶段变化时，使用 `TaskStop` 终止该后台任务。若宿主只停止轮询而没有终止进程，使用相同的标准 Bash 入口和输出中的 Request ID 执行 `--continue <request-id>` 续传，或执行 `--cancel <request-id>` 明确取消；续传时仍显式传入同一个工作空间 `outputs/`。
- 在成功读取图片、后台任务明确失败或达到上述有界终止条件之前，不得向用户发送结束当前回合的最终回复。进度消息之后必须继续操作同一个任务 ID。

## 结果解读

每次成功编辑会打印：

- `图片已修改: <绝对路径>` — 修改后的图片文件
- `原图` — 源图片路径
- `提示词`、`种子`、`参数`、`设备` — 本次输入
- `耗时` — 加载 / 推理 / 保存耗时

成功后必须用 `Read` 打开 `图片已修改:` 后的绝对路径。图片位于工作空间 `outputs/`，因此应能进入当前任务的产物/文件列表；不要用 `TaskOutput` 代替图片读取。

常见错误码：

- `BAD_IMAGE` — 源图片缺失、不可读或非法
- `BAD_PROMPT` — 提示词为空或非字符串
- `GENERATION_FAILED` — OpenVINO 推理阶段异常
- `SAVE_FAILED` — 输出 PNG 无法写入

## 运行前提与边界

- 仅支持已安装 Git Bash 的 Windows Intel AIPC。WorkBuddy 未提供 Bash 工具时属于不兼容宿主；不要尝试 PowerShell 回退。
- 首次使用需联网安装 Python 依赖；仅在指定目录、共享目录和旧版目录中都没有发现模型时下载模型。
- 正常空闲时模型仍驻留 GPU 300 秒；推理服务退出后，server-dog 也随即退出，不跨 WorkBuddy 会话驻留。取消操作只取消对应 Request ID，并在推理服务仍存活时保留它供其他任务使用。客户端异常消失且没有发出取消、IPC 超时或服务连续失联时才提前回收服务。
- WorkBuddy 只使用 Bash 入口；不存在第二套运行或回退流程。
- 非在线图像生成代理，全部推理本地完成。
- 不提供 inpainting mask、ControlNet、LoRA 等精细控制（全图重绘，无局部遮罩）。
- 商品包装文字、人像身份细节和严重破损照片的缺失区域可能受全图重绘影响，交付前需检查。
- 失败时不要静默回退到在线图像编辑服务，需如实告知用户失败原因。
