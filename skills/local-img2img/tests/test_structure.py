from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


def read(rel_path: str) -> str:
    return (SKILL_ROOT / rel_path).read_text(encoding="utf-8")


def test_required_skill_files_exist():
    for rel_path in [
        "info.json",
        "SKILL.md",
        "scripts/client.py",
        "scripts/server.py",
        "scripts/run.ps1",
        "tests/test.ps1",
        "tests/tui_test.py",
    ]:
        assert (SKILL_ROOT / rel_path).exists(), rel_path


def test_img2img_identity_and_model_are_configured():
    info = read("info.json")
    assert '"snake7gun/FLUX.2-klein-4B-int4-ov"' in info
    assert '"dir_name": "FLUX.2-klein-4B-int4-ov"' in info
    assert "text_encoder/openvino_model.bin" in info
    assert "transformer/openvino_model.bin" in info
    assert "name: local-img2img" in read("SKILL.md")
    # `meta.json` was removed during v2.4 compliance migration; the plugin.json
    # in the parent expert package is now the canonical identity record.
    plugin = (SKILL_ROOT.parent.parent / ".codebuddy-plugin" / "plugin.json").read_text(
        encoding="utf-8"
    )
    assert "./skills/local-img2img" in plugin
    assert "去杂物" in plugin or "Clutter Removal" in plugin
    # Four capability prompts (not limited to three).
    assert plugin.count('"zh":') >= 8


def test_client_server_use_img2img_runtime_contract():
    client = read("scripts/client.py")
    server = read("scripts/server.py")
    run_ps1 = read("scripts/run.ps1")

    assert r"\\.\pipe\img2img" in client
    assert r"\\.\pipe\img2img" in server
    assert "AUTHKEY = b\"img2img-local\"" in client
    assert "AUTHKEY = b\"img2img-local\"" in server
    assert "OPENVINO_TELEMETRY_OPT_OUT" in client
    assert "Image2ImagePipeline" in server
    assert "reference_image_path" in server
    assert "ov.Tensor" in server
    assert "ImagePath" in run_ps1
    assert "UserPrompt" in run_ps1
