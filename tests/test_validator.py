import pathlib

import pytest

from stackd.config.loader import load_config
from stackd.planner import plan_transition
from stackd.validator import validate_profile

CFG_DIR = pathlib.Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(scope="module")
def cfg():
    return load_config(CFG_DIR)


def test_config_loads(cfg):
    assert set(cfg.pools) == {"cuda_vram", "host_unified"}
    assert set(cfg.devices) == {"cuda0", "igpu0"}
    assert set(cfg.profiles) == {"chat", "coding"}


def test_chat_fits(cfg):
    r = validate_profile(cfg, "chat")
    assert r.ok
    cuda = next(p for p in r.pools if p.pool == "cuda_vram")
    assert cuda.used_gib == pytest.approx(88.0, abs=0.5)  # 42 chat + 46 image
    unified = next(p for p in r.pools if p.pool == "host_unified")
    # 28 reserve + 4 slack + (autocomplete 2 + headroom 3 = 5 iGPU) + (3 + 18) cuda0 RAM
    assert unified.used_gib == pytest.approx(58.0, abs=0.5)
    assert unified.ok


def test_chat_flags_image_llm_contention(cfg):
    r = validate_profile(cfg, "chat")
    assert any("contention on cuda0" in f for f in r.flags)


def test_coding_fits_and_keeps_autocomplete(cfg):
    r = validate_profile(cfg, "coding")
    assert r.ok
    assert "coding" in r.resident
    assert "code-autocomplete" in r.resident  # kept via mask: keep@igpu0
    assert "chat" not in r.resident
    cuda = next(p for p in r.pools if p.pool == "cuda_vram")
    assert cuda.used_gib == pytest.approx(88.0, abs=0.5)


def test_coding_fits_on_its_own(cfg):
    r = validate_profile(cfg, "coding")
    assert r.ok
    cuda = next(p for p in r.pools if p.pool == "cuda_vram")
    assert cuda.headroom_gib < 10  # coding fills cuda0


def test_plan_chat_to_coding_is_a_delta(cfg):
    p = plan_transition(cfg, "chat", "coding")
    assert p.teardown == ["chat"]
    assert sorted(p.spawn) == ["coding"]
    assert p.keep == ["code-autocomplete"]  # untouched by the switch
