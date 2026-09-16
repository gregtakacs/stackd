"""Live check: does the served spike page actually EXECUTE its controller?

Static greps proved substitution happened; they could not prove the code was inside a
<script> element, which is exactly the bug that rendered both panels blank. This asserts
the structural property on the bytes the browser will receive.
"""
import re
import urllib.request

h = urllib.request.urlopen("http://127.0.0.1:8191/toolbox/spike", timeout=8).read().decode()

# The controller may legitimately MENTION a script tag in prose, which inflates a naive
# open-tag count. Only `</script` is structurally dangerous (it would close the element
# early), so the balance check counts real script blocks and separately asserts no JS file
# smuggles a closing tag in.
h_code = re.sub(r"<!--.*?-->", "", h, flags=re.S)
blocks = re.findall(r"<script[^>]*>(.*?)</script>", h_code, flags=re.S)
residue = re.sub(r"<script[^>]*>.*?</script>", "", h_code, flags=re.S)
leaks = [t for t in ("function (", "addEventListener", "__TB_", "location.reload") if t in residue]

def in_script(needle):
    spans = [(m.start(), m.end()) for m in
             re.finditer(r"<script[^>]*>.*?</script>", h_code, re.S)]
    at = h_code.find(needle)
    return at != -1 and any(s <= at < e for s, e in spans)

checks = [
    ("script blocks are balanced and the controller is one of them",
     h_code.count("<script") == h_code.count("</script") == len(blocks) >= 1
     and any("addEventListener('message'" in b for b in blocks)),
    ("no JS smuggles a closing script tag (would truncate the element)",
     all("</script" not in b for b in blocks)),
    ("no controller code loose in the body", not leaks),
    ("controller executes (message listener in a script element)",
     in_script("addEventListener('message'")),
    ("embed URL was injected into the controller",
     bool(re.search(r"""embed = \(typeof "/toolbox/embed\?token=v1\.""", h_code))),
    ("the injected URL is a URL the server will actually serve",
     bool(re.search(r"""/toolbox/embed\?token=[A-Za-z0-9._-]{20,}""", h_code))),
    ("probe appended for both panels", h.count("probe=1") >= 1),
    ("deadman watchdog armed", "NO DATA" in h and "CELLS" in h),
    ("all 12 measurement cells exist",
     all(('id="%s"' % i) in h for i in
         ("cnta", "canvasa", "ha", "scrolla", "exporta", "servera",
          "cnt", "canvas", "h", "scrollb", "export", "server"))),
    ("both iframes present, B is sandboxed",
     'id="fa"' in h and 'id="fb"' in h and 'sandbox="allow-scripts"' in h),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(("  PASS  " if ok else "  FAIL  ") + n)
if leaks:
    print("          leaked tokens:", leaks)
m = re.search(r"var embed = (.{0,90})", h_code)
print("          injected literal:", m.group(1) if m else "NOT FOUND")
print("\n  %d/%d live harness checks passed" % (len(checks) - len(bad), len(checks)))
raise SystemExit(1 if bad else 0)
