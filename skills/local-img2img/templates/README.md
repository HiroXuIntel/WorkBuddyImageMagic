# 七个场景模板

本专家不依赖 iCreator / 工作模式胶囊。WorkBuddy 发起请求时，先用 `Write` 创建结构化 JSON，再调用一次标准 Bash 入口；不要把图片路径或提示词拼进 shell。

请求文件格式：

```json
{
  "image_path": "C:\\absolute\\path\\input.png",
  "prompt": "把背景改成海边日落，保留主体外形和细节，光影自然衔接",
  "scene": "background-swap"
}
```

七个场景与模板一一对应：

| 序号 | 场景 | `scene` | 提示词模板 |
| --- | --- | --- | --- |
| 1 | 换背景 | `background-swap` | `01-swap-background.txt` |
| 2 | 去标记 | `mark-removal` | `02-remove-watermark.txt` |
| 3 | 证件照换底 | `id-photo-background` | `03-id-photo-recolor.txt` |
| 4 | 去杂物 | `clutter-removal` | `04-remove-clutter.txt` |
| 5 | 商品场景 | `product-scene` | `05-product-scene.txt` |
| 6 | 自然人像美颜 | `natural-portrait` | `06-natural-portrait.txt` |
| 7 | 老照片修复 | `old-photo-restoration` | `07-old-photo-restoration.txt` |

WorkBuddy 标准入口：

```bash
bash "${CODEBUDDY_SKILL_DIR}/scripts/run.sh" --request-file ".photo-magic-request-<REQUEST_UUID>.json" "<REQUEST_UUID>"
```

请求文件使用当前 WorkBuddy 工作空间的相对路径；输出自动保存到同一工作空间的 `outputs/`。本地开发者可显式设置 `LOCAL_IMG2IMG_DEV_MODE=1`，再使用 `bash scripts/run.sh --dev-direct "IMAGE" "PROMPT"` 做可信参数联调；这不是 WorkBuddy 的标准调用方式。模型下载续传使用同一运行数据目录和 `--continue <request-id>`。
