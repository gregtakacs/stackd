Open the Comfy Toolbox mask editor for the image the user is looking at. This tool renders
NOTHING and takes no parameters of any kind -- it resolves the chat's most recent image the
same way edit_image does (server-side auto-detect via the real conversation tree, the
CALLING user's own registered API key, no model-suppliable image reference) and returns a
markdown link that opens the mask editor ONCE: the link carries a short one-time access
CODE that dies on the first render submission and expires ~15 minutes after this call. If
the user's link has gone stale, call this tool again for a fresh one -- that is the intended
recovery, not an error state.

COPY THE LINK OUT EXACTLY AS GIVEN. It is short on purpose (a ~10-character code) because
you are the only thing standing between the tool output and the user's browser, and a
single altered or dropped character makes the editor reject a link that was minted seconds
ago -- which looks to the user like a broken feature, not like a typo. Do not retype,
re-wrap, shorten, URL-decode, split across lines or "correct" it; emit the markdown link the
tool gave you, whole, and do not restate the URL in prose.

Use this, and NOT edit_image, whenever the user wants to control the masked area THEMSELVES:
any phrasing like "let me pick the area", "I want to paint the mask", "let me choose
exactly what changes", "open the editor / the toolbox / mask select on this", or an edit
request where the user is clearly better served by pointing at the region than by describing
it ("the thing on the left, you know which one" -- do not guess with a SAM segmentation,
hand them the brush). Also use it when the user has ALREADY been unhappy with a
text-described target_region from edit_image in this chat: a mis-segmented edit should
escalate to painted control, not to another round of guessing.

Do NOT use this tool for an edit you can already act on: if the user describes the target
clearly and wants a result, not an editor, that is edit_image (masked) or generate_image --
calling this instead stalls a workable request behind a UI they didn't ask for. It is also
never a preview, a history viewer, or a way to act on an OLDER image: like edit_image it
always targets the current branch's most recent image, and there is no image parameter by
design.

Exactly one call opens one editor session. Do not call it twice for the same request and do
not call edit_image alongside it for the same edit in the same turn -- the user either
drives or you drive, not both. After calling, put the link in your reply with one line of
guidance: paint the area to change; the Selection box (tap an object with the select tool)
holds the edge/feather tuning; Render runs the edit and the artifact saves into their own
Open WebUI files AND lands back in this conversation automatically as a new message when
the render finishes (the link's session already carries this chat's id — do not ask the
user to paste it back, and do not announce a result you did not produce: the toolbox
posts its own result line). If the tool returns a plain-text
failure (no image in chat, not registered, toolbox not mounted), relay it honestly and
fall back to edit_image only if the user's request is describable without painted control.
