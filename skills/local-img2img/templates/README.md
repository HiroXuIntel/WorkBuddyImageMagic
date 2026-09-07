# 本地测试模板（复制即用）

本专家按 WorkBuddy 专家规范交付，**不依赖 iCreator / 工作模式胶囊**。
测试时不必点正式胶囊生成模板，把下面命令整段复制到终端即可。

把 `IMAGE` 换成你的图片绝对路径。只需调用 **一次** `run.ps1`（它会自动做环境检查并启动推理客户端；下载未完成时也会自动续跑，不必再单独调 `client.py`）。

```powershell
cd skills/local-img2img
```

## 1. 换背景

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run.ps1 "IMAGE" "帮我把这张照片的背景换成海边日落，保持主体人物/动物不变，光影自然过渡"
```

## 2. 去水印

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run.ps1 "IMAGE" "帮我去掉这张图片右下角的水印文字，让被遮挡的区域自然还原不留痕迹"
```

## 3. 证件照换底色

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run.ps1 "IMAGE" "帮我把这张证件照的底色换成蓝色，保持人像五官不变，发丝边缘清晰（非正式制证）"
```

## 4. 去杂物

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run.ps1 "IMAGE" "帮我把这张照片里多余的杂物、路人和电线去掉，让画面干净整洁"
```

## 内置样例图

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run.ps1 "tests/dog.png" "make the background a sunny beach"
```

模型下载若超时中断，用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run.ps1 --continue
```
