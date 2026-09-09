# P图管家（Photo Magic）

一句话修图 AI 专家（WorkBuddy Agent 型）。基于 Intel AIPC 本地图生图（FLUX.2-klein OpenVINO）完成：换背景、去标记、证件照换底色、去杂物、商品场景、人像自然美颜、老照片修复。推理不上传云端。

本仓库只交付**专家包**，不包含 iCreator / 工作模式胶囊前端。七个能力通过 `plugin.json` 的 `tags`、`quickPrompts` 与本地提示词模板保持一致。

## 目录结构

```
photo-magic/
├── .codebuddy-plugin/
│   └── plugin.json              # 专家包配置（7 个 tags 与 7 条 quickPrompts 一一对应）
├── avatars/
│   └── expert.png
├── agents/
│   └── photo-magic.md
├── skills/
│   └── local-img2img/
│       ├── SKILL.md
│       ├── info.json
│       ├── requirements.txt / requirements.lock
│       ├── templates/           # 复制即测的七条提示词模板
│       ├── scripts/
│       └── bin/
└── README.md
```

## 运行前提

- **仅支持已安装 Git Bash 的 Windows Intel AIPC**；WorkBuddy 没有 Bash 工具时不兼容，不使用 PowerShell 回退。
- **首次使用的联网需求**：需要安装 Python 依赖；引擎会原地复用 `~/.openvino/models` 或旧版 `~/.openvino/photo-magic/models` 中已有的 FLUX.2-klein 模型，也可用 `LOCAL_IMG2IMG_MODEL_DIR` 指定其他目录。模型不会复制；没有发现模型时才下载到 `~/.openvino/models`。宿主未提供数据目录变量时，虚拟环境、日志与运行缓存稳定保存在 `~/.openvino/photo-magic`，成品保存到当前项目 `outputs/`。
- 缺少 Python 环境时，运行数据目录至少预留 4 GB；只有需要下载模型时，模型所在磁盘才要求至少预留 20 GB。
- 内存建议 ≥ 8.5 GB 可用。
- 冷启动握手最长等待 90 秒，但服务就绪会立即继续；后台服务使用 uv 对应的真实 CPython 启动，关闭时优雅退出并释放 GPU。推理服务退出后 server-dog 同步退出，不跨 WorkBuddy 会话驻留。启动日志按进程隔离，旧的 oneDNN/OpenCL 探测信息不会被当成本次错误。
- 冷启动握手最多等待 90 秒，普通 IPC 仍保持 10 秒。Intel OpenCL GPU 暂时不可用时只延迟重启一次；仍失败会明确提示检查 GPU 占用或 Intel 显卡/OpenCL 驱动，不会无限重试或静默回退。

## 标准调用

见 [`skills/local-img2img/templates/README.md`](skills/local-img2img/templates/README.md)。示例：

WorkBuddy 使用 `Write` 在当前工作空间创建结构化请求文件，再用相对路径通过 `--request-file` 调用；不依赖 `CODEBUDDY_PROJECT_DIR` 或 `CODEBUDDY_PLUGIN_DATA` 的 shell 展开。完整格式见模板说明。不要直接调用 `client.py`，也不要切换到 PowerShell。

成功输出类似：

```
图片已修改: C:\...\outputs\dog_edited_2026XXXX_XXXXXX_ab12cd34.png
```

WorkBuddy 专家随后使用 `Read` 读取该绝对路径并直接交付图片；`TaskOutput` 只负责取得后台命令输出。

模型下载超时后，在同一工作空间继续：

```bash
bash scripts/run.sh --continue <request-id>
```

## 头像说明

`avatars/expert.png` 为 512×512 PNG 专家头像，符合 WorkBuddy 提交规格。

## 依赖与许可证

本仓库不再声明未经核实的统一许可证。内置二进制、运行时下载依赖和模型的来源、版本、许可证与完整性信息见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 和 `skills/local-img2img/bin/checksums.sha256`。
