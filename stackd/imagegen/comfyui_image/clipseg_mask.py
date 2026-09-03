"""
CLIPSegMask -- text -> mask for the comfyui-mcp masked-edit pipeline.

Replaces ComfyUI-Florence2's Florence2Run in edit/flux2-klein-inpaint.json:
Florence-2's custom trust_remote_code modeling returns garbage segmentation on
ROCm/gfx1151. This is stock transformers.CLIPSegForImageSegmentation
(CIDAS/clipseg-rd64-refined, ~150 MB, auto-downloaded to the HF cache) -- no
custom code, works on ROCm. Output is a single MASK at index 0 (Florence2Run
had it at index 1 -- the graph + server._submit_edit account for that).
"""
import torch
import torch.nn.functional as F

_MODEL = "CIDAS/clipseg-rd64-refined"
_state = {"proc": None, "model": None, "device": None}


def _load():
    if _state["model"] is None:
        from transformers import CLIPSegProcessor, CLIPSegForImageSegmentation
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        _state["proc"] = CLIPSegProcessor.from_pretrained(_MODEL)
        _state["model"] = CLIPSegForImageSegmentation.from_pretrained(_MODEL).to(dev).eval()
        _state["device"] = dev
    return _state["proc"], _state["model"], _state["device"]


class CLIPSegMask:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "text": ("STRING", {"default": "", "multiline": True}),
                "threshold": ("FLOAT", {"default": 0.40, "min": 0.0, "max": 1.0, "step": 0.01}),
                # `dilation` / `blur` are now FLOORS (minimum px / sigma). The
                # actual grow + feather are sized to the segmented object -- see
                # grow_frac / feather_frac below and the run() comment.
                "blur": ("FLOAT", {"default": 3.0, "min": 0.0, "max": 64.0, "step": 0.5}),
                "dilation": ("INT", {"default": 3, "min": 0, "max": 128}),
            },
            "optional": {
                # Proportional geometry: grow_px  = grow_frac  * sqrt(mask area),
                #                        feather  = feather_frac * sqrt(mask area) (gaussian sigma)
                # clamped to [floor, max_*]. sqrt(area) is the object's
                # characteristic size, so a small target gets a small margin and
                # a large one a larger (but capped) margin -- a FIXED px grow/
                # feather made small objects balloon to fill an oversized mask.
                "grow_frac": ("FLOAT", {"default": 0.07, "min": 0.0, "max": 1.0, "step": 0.005}),
                "feather_frac": ("FLOAT", {"default": 0.05, "min": 0.0, "max": 1.0, "step": 0.005}),
                "max_grow": ("INT", {"default": 80, "min": 0, "max": 512}),
                "max_feather": ("FLOAT", {"default": 48.0, "min": 0.0, "max": 256.0, "step": 1.0}),
            },
        }

    RETURN_TYPES = ("MASK", "IMAGE")
    RETURN_NAMES = ("mask", "heatmap")
    FUNCTION = "run"
    CATEGORY = "mask"

    def run(self, image, text, threshold, blur, dilation,
            grow_frac=0.07, feather_frac=0.05, max_grow=80, max_feather=48.0):
        proc, model, dev = _load()
        # ComfyUI IMAGE: (B,H,W,C) float 0..1. Handle the first frame only.
        img = image[0]
        h, w = int(img.shape[0]), int(img.shape[1])
        pil = _to_pil(img)

        inputs = proc(text=[text or "object"], images=[pil], return_tensors="pt", padding=True)
        inputs = {k: v.to(dev) for k, v in inputs.items()}
        with torch.no_grad():
            logits = model(**inputs).logits  # (1, s, s) or (s, s)
        if logits.dim() == 2:
            logits = logits.unsqueeze(0)
        heat = torch.sigmoid(logits).unsqueeze(1).float()          # (1,1,s,s)
        heat = F.interpolate(heat, size=(h, w), mode="bilinear", align_corners=False)
        heat = heat[0, 0].clamp(0, 1).cpu()                        # (h,w)

        # Adaptive threshold: normalise the heatmap to its own peak before
        # thresholding, so a WEAK-but-real detection -- an imprecise or slightly
        # off-target prompt ("the Lamborghini" on a car that doesn't read as
        # one, "the skyscraper", a pet's proper name) -- still fills the whole
        # object instead of only its most-on-concept part. max(peak, 0.5) is a
        # no-op for a confident detection (peak ~0.9) and a floor that stops a
        # no-detection frame (peak well under 0.5) from yielding a phantom mask.
        peak = float(heat.max())
        m = ((heat / max(peak, 0.5)) >= float(threshold)).float()

        # Morphological CLOSE (done at low res so a large bridging radius is
        # ~free): CLIPSeg often returns a FRAGMENTED response for an imprecise
        # prompt -- e.g. a blob on a car's front + a separate blob on its rear,
        # the mid-section missed -- and that seam then keeps the original
        # content. Dilate-then-erode by the same amount merges nearby fragments
        # and fills interior holes with no net size change; a no-op on an
        # already-solid mask.
        _hh, _ww = m.shape
        _s = F.interpolate(m[None, None], size=(128, 128), mode="bilinear", align_corners=False)
        _s = (_s > 0.3).float()
        _k = 15  # ~7px radius at 128 -> ~70px at a 1312 canvas: bridges the typical gap
        _s = F.max_pool2d(_s, kernel_size=_k, stride=1, padding=_k // 2)
        _s = -F.max_pool2d(-_s, kernel_size=_k, stride=1, padding=_k // 2)
        m = (F.interpolate(_s, size=(_hh, _ww), mode="bilinear", align_corners=False)[0, 0] > 0.3).float()

        # Size the grow + feather to the OBJECT, not to a fixed pixel count.
        area = float(m.sum())
        char = area ** 0.5 if area > 0 else 0.0  # characteristic size of the mask
        grow_px = int(min(int(max_grow), max(int(dilation), round(float(grow_frac) * char))))
        feather_sigma = float(min(float(max_feather), max(float(blur), float(feather_frac) * char)))

        if grow_px > 0:
            k = grow_px * 2 + 1
            m = F.max_pool2d(m[None, None], kernel_size=k, stride=1, padding=grow_px)[0, 0]
        if feather_sigma > 0:
            m = _gaussian_blur(m, feather_sigma)
        m = m.clamp(0, 1)

        mask = m.unsqueeze(0)                                       # (1,h,w) -- ComfyUI MASK
        heatmap_img = heat.unsqueeze(-1).repeat(1, 1, 3).unsqueeze(0)  # (1,h,w,3) IMAGE
        return (mask, heatmap_img)


def _to_pil(img_hwc):
    from PIL import Image
    import numpy as np
    a = (img_hwc.clamp(0, 1).cpu().numpy() * 255.0).round().astype("uint8")
    return Image.fromarray(a)


def _gaussian_blur(m, sigma):
    radius = max(1, int(round(sigma * 2)))
    x = torch.arange(-radius, radius + 1, dtype=torch.float32)
    g = torch.exp(-(x ** 2) / (2 * sigma * sigma))
    g = (g / g.sum())
    k = m[None, None]
    k = F.conv2d(k, g.view(1, 1, 1, -1), padding=(0, radius))
    k = F.conv2d(k, g.view(1, 1, -1, 1), padding=(radius, 0))
    return k[0, 0]


NODE_CLASS_MAPPINGS = {"CLIPSegMask": CLIPSegMask}
NODE_DISPLAY_NAME_MAPPINGS = {"CLIPSegMask": "CLIPSeg Mask (text)"}
