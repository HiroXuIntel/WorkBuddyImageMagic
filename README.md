# P图管家（Photo Magic）

一句话修图 AI 专家。基于 Intel AIPC 本地图生图（FLUX.2-klein OpenVINO）完成：换背景、去水印、证件照换底色、去杂物。推理不上传云端。

## 目录结构

```
photo-magic/
├── .codebuddy-plugin/
│   └── plugin.json              # 专家包配置
├── avatars/
│   └── expert.png               # 专家头像（512×512 PNG，占位）
├── agents/
│   └── photo-magic.md          # P图管家 Agent 定义
├── skills/
│   └── local-img2img/           # 本地图生图 Skill
│       ├── SKILL.md
│       ├── info.json            # 技能运行配置
│       ├── requirements.txt     # Python 依赖
│       ├── scripts/             # 技能脚本
│       │   ├── client.py
│       │   ├── server.py
│       │   ├── server-dog.py
│       │   ├── model_download.py
│       │   ├── get_gpu_mem.py
│       │   ├── run.ps1          # 入口脚本
│       │   └── install-env.ps1  # 环境初始化脚本
│       └── bin/                 # 技能私有运行时（platform.exe / uv.exe / openvino_genai）
└── README.md
```

> 说明：`bin/` 放在 `skills/local-img2img/bin/` 而非插件根 `bin/`，是因为其中 `openvino_genai/` 是 FLUX.2-klein 模型推理专属运行时，`platform.exe` 与 `uv.exe` 均为该 skill 的安装/检测辅助工具，并非跨技能通用工具。规范中插件根 `bin/` 用于"跨技能通用工具"，本专家包暂无需此类工具，故未设插件根 `bin/`。

## 运行前提

- **仅支持 Intel AIPC 平台（Windows）**。运行时需 `bin/platform.exe --is-aipc` 返回 1；非 AIPC 平台直接拒绝执行。
- **首次使用需要联网**——运行期完整组件由 `install-env.ps1` 与 `run.ps1` 自动装配：

  | 组件 | 大小 | 由谁装 | 备注 |
  |---|---|---|---|
  | VC++ 2015-2022 x64 运行时 | ~25 MB | `install-env.ps1` Step 0 | 缺失会触发 `WinError 1114` |
  | uv 包管理器（`bin/uv.exe`） | ~62 MB | `install-env.ps1` Step 1 | 已带则跳过；缺失时从 gitcode 镜像下载 |
  | Python 3.11 虚拟环境 | ~50 MB | `install-env.ps1` Step 2 | 路径 `~/.openvino/venv/img2img` |
  | Python 依赖（`requirements.txt`） | ~2 GB | `install-env.ps1` Step 3 | 含 PyTorch / OpenVINO / Diffusers |
  | FLUX.2-klein OpenVINO 模型 | ~6 GB | `run.ps1` 首次调用 | `snake7gun/FLUX.2-klein-4B-int4-ov` |
  | `bin/openvino_genai/` 原生模块 | 已打包 | （随专家包） | **仅兼容 Python 3.11**（cp311 .pyd） |
  | **合计首次下载/安装** | **~8-9 GB** | | |

- **内存**：建议 ≥ 8.5 GB 可用（模型默认在 GPU 上推理）。
- **故障排查**：若 `import openvino_genai` 失败，确认 venv 是 3.11（`python --version`），非 3.11 时该 .pyd 无法加载；删除 `~/.openvino/venv/img2img` 后重跑 `install-env.ps1` 即可。

## 快速测试

```powershell
cd skills/local-img2img
powershell -ExecutionPolicy Bypass -File scripts/run.ps1 "tests/dog.png" "make the background a sunny beach"
```

成功后会输出类似：

```
图片已修改: C:\...\dog_edited_2026XXXX_XXXXXX.png
```

## 头像说明

`avatars/expert.png` 为 512×512 PNG 占位头像（约 9 KB，符合尺寸/大小规范），已满足合规要求。正式上架前建议替换为专业漫画/插画风格头像。

## 许可证

模型与运行时采用 Intel OBL Distribution 许可证分发，详见 `skills/local-img2img/SKILL.md`。
