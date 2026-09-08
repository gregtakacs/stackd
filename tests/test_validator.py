import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _env  # noqa: F401,E402  — config/*.yaml interpolates ${CUDA_VRAM_GIB}… and
#      errors on anything unset; every other suite that loads config imports this.

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
    assert set(r.resident) == {"chat", "code-autocomplete"}
    cuda = next(p for p in r.pools if p.pool == "cuda_vram")
    # chat is the ONLY cuda0 member now: the image tier left the profile (it is
    # elastic — config/media/image.yaml fills whatever VRAM is left over), and
    # chat's own budget was retuned to 30. This test used to pin "42 chat +
    # 46 image = 88", a composition the shipped config no longer describes, so it
    # asserted a placement that cannot happen. Both numbers now come from the same
    # config the validator read: a budget retune stays green, a second cuda0
    # tenant (or the image tier creeping back into the profile) still trips it.
    assert cuda.used_gib == pytest.approx(cfg.models["chat"].budget.vram_gib, abs=0.1)
    unified = next(p for p in r.pools if p.pool == "host_unified")
    # The unified pool's charge is the sum of its stated parts — reserve, slack,
    # the iGPU's GTT window, cuda0's RAM — and those parts must add up to it.
    assert unified.breakdown
    assert unified.used_gib == pytest.approx(sum(unified.breakdown.values()), abs=0.1)
    assert set(unified.breakdown) == {"host_reserve", "load_slack", "vram:igpu0 (≤90)",
                                      "ram:cuda0"}
    assert unified.ok


def test_igpu_budget_is_only_clamped_by_a_measured_window(cfg):
    """The GTT clamp (fit.budget_facts) must be inert when no window is measurable
    — the reference env sets STACKD_DISABLE_GPU_AUTODETECT=1, so the config budget
    stands and nothing reports itself as clamped."""
    r = validate_profile(cfg, "chat")
    ig = next(d for d in r.devices if d.device == "igpu0")
    assert ig.budget_gib == pytest.approx(90.0)
    assert ig.budget_effective_gib == pytest.approx(90.0)
    assert ig.gtt_window_gib is None and not ig.clamped
    assert not any("clamp" in f for f in r.flags)


def test_chat_leaves_the_image_tier_nothing_to_contend_with(cfg):
    """Used to assert a "contention on cuda0" flag, which existed only while an
    image model shared cuda0 with the LLM inside the profile. The elastic tier
    took that arrangement out of the profile, so the honest version of this check
    is that a healthy reference profile raises NO flags at all — the flag machinery
    still fires (see the clamp test above and smoke_web's), it just has nothing to
    fire on here."""
    r = validate_profile(cfg, "chat")
    assert r.flags == []


def test_coding_keeps_autocomplete_resident(cfg):
    r = validate_profile(cfg, "coding")
    assert r.ok
    assert "coding" in r.resident
    assert "code-autocomplete" in r.resident  # listed by both profiles: stays on igpu0
    assert "chat" not in r.resident
    cuda = next(p for p in r.pools if p.pool == "cuda_vram")
    assert cuda.used_gib == pytest.approx(cfg.models["coding"].budget.vram_gib, abs=0.1)


def test_coding_fits_on_its_own(cfg):
    r = validate_profile(cfg, "coding")
    assert r.ok
    cuda = next(p for p in r.pools if p.pool == "cuda_vram")
    assert cuda.headroom_gib < 10  # coding fills cuda0


def test_plan_chat_to_coding_is_a_delta(cfg):
    p = plan_transition(cfg, "chat", "coding")
    assert p.teardown == ["chat"]
    assert sorted(p.spawn) == ["coding"]
    # code-autocomplete stays resident on igpu0 across the switch, but it is
    # RE-CREATED rather than kept: solve() hands out llamacpp's dynamic ports in
    # profile order, so the stack is 11501 under `chat` and 11500 under `coding`,
    # and `port` is deliberately part of model_identity() — a container left
    # running on a port the config no longer assigns was "warming" forever
    # (2026-09-04, see solver.model_identity). The old `keep == [...]` line here
    # asserted the promise in config/profiles/coding.yaml, which the port
    # assignment quietly invalidated.
    assert p.keep == []
    assert p.reload == ["code-autocomplete"]
