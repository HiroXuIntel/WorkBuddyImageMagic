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

- **仅支持 Intel AIPC 平台（Windows）**。运行前会调用 `bin/platform.exe --is-aipc` 校验。
- 首次使用需要联网：
  - 通过 `scripts/install-env.ps1` 安装 Python 虚拟环境与 `requirements.txt` 依赖；
  - 通过 `scripts/run.ps1` 首次调用时自动下载 FLUX.2-klein OpenVINO 模型。
- 内存：建议 ≥ 8.5 GB 可用（模型默认在 GPU 上推理）。

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
