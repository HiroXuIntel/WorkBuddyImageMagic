# P图管家（Photo Magic）

一句话修图 AI 专家（WorkBuddy Agent 型）。基于 Intel AIPC 本地图生图（FLUX.2-klein OpenVINO）完成：换背景、去水印、证件照换底色、去杂物。推理不上传云端。

本仓库只交付**专家包**，不包含 iCreator / 工作模式胶囊前端。四个能力通过 `plugin.json` 的 `quickPrompts` 与本地模板测试。

## 目录结构

```
photo-magic/
├── .codebuddy-plugin/
│   └── plugin.json              # 专家包配置（含 4 条 quickPrompts）
├── avatars/
│   └── expert.png
├── agents/
│   └── photo-magic.md
├── skills/
│   └── local-img2img/
│       ├── SKILL.md
│       ├── info.json
│       ├── requirements.txt
│       ├── templates/           # 复制即测的四条提示词模板
│       ├── scripts/
│       └── bin/
└── README.md
```

## 运行前提

- **仅支持 Intel AIPC 平台（Windows）**。
- **仅首次使用需要联网**：装 Python 依赖 + 下载 FLUX.2-klein 模型。之后应复用 `~/.openvino/venv/img2img` 与已晋级的模型目录，不再全量重装。
- 内存建议 ≥ 8.5 GB 可用。

## 快速测试（复制模板，不点胶囊）

见 [`skills/local-img2img/templates/README.md`](skills/local-img2img/templates/README.md)。示例：

```powershell
cd skills/local-img2img
powershell -ExecutionPolicy Bypass -File scripts/run.ps1 "tests/dog.png" "make the background a sunny beach"
```

成功输出类似：

```
图片已修改: C:\...\dog_edited_2026XXXX_XXXXXX.png
```

模型下载超时后继续：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run.ps1 --continue
```

## 头像说明

`avatars/expert.png` 为 512×512 PNG 占位头像。正式上架前建议换成专业插画风格。

## 许可证

模型与运行时采用 Intel OBL Distribution 许可证分发，详见 `skills/local-img2img/SKILL.md`。
