# Third-party notices

本文件记录专家包直接携带或运行时获取的第三方组件。专家自身代码的授权由发布者另行确定；不得用第三方许可证替代专家代码许可证。

## 随包二进制

- `skills/local-img2img/bin/uv.exe`: uv 0.10.12，Astral，MIT OR Apache-2.0。来源项目：<https://github.com/astral-sh/uv>。
- `skills/local-img2img/bin/openvino_genai/*`: OpenVINO GenAI 2026.2 Windows/Python 3.11 运行时，Apache-2.0。来源项目：<https://github.com/openvinotoolkit/openvino.genai>。
- 所有随包文件在启动前按 `skills/local-img2img/bin/checksums.sha256` 校验；校验失败时拒绝执行。

## 运行时依赖与模型

- Python 直接依赖在 `skills/local-img2img/requirements.txt` 固定版本，完整传递依赖及下载包哈希在 `skills/local-img2img/requirements.lock` 固定。各包许可证随安装包提供，发布前仍应由法务/合规流程复核。
- 模型 `snake7gun/FLUX.2-klein-4B-int4-ov` 不随专家包分发，首次使用时从 ModelScope 下载；下载固定到提交 `d20758a380a0169b8c94116078f0613b26362bed`。模型仓库自带的模型卡和许可证文件为最终适用条款。

## 发布检查

每次替换 `bin/` 文件、升级依赖或更新模型提交时，必须同步更新版本说明、许可证复核结果和 SHA-256 清单；不能只改版本号。
