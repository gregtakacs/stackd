import os, sys, spike
HERE = spike.HERE
src = os.path.join(HERE, "out", "gate_a.png")
res = os.path.join(HERE, "out-hip-1024", "gate_c.png")
mask = os.path.join(HERE, "out-hip-1024", "mask_box.png")
print("src", os.path.exists(src), "res", os.path.exists(res), "mask", os.path.exists(mask))
print("gate_c outside-mask:", spike.region_diff(src, res, mask))
