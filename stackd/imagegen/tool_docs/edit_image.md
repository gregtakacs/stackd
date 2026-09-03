Transform an existing image according to a text prompt using the local Flux.2 Klein 9B
ComfyUI pipeline. Use this for targeted edits: adding, removing, replacing, or restyling
content in an image the user already provided. There is no negative_prompt parameter --
both underlying pipelines are distilled variants with no negative-prompt input.

There is no image parameter of any kind -- this tool ALWAYS acts on the most recent image
in this chat's current branch, auto-detected server-side by walking the real conversation
tree (requires Open WebUI's ENABLE_FORWARD_USER_INFO_HEADERS to be on; resolves the
CALLING user's own chats via their own registered API key -- if nothing can be found, you
get a clear error back, never a silent wrong image). There is deliberately no way to
target a different, older image instead -- confirmed in practice this is a real, not just
theoretical, footgun: given the ability to pass one explicitly, a model has reached for
the literal original upload instead of a generate_image REIMAGINE's own (pixel-unrelated)
output earlier in the same conversation, silently discarding every change REIMAGINE made.
If the user wants to act on a DIFFERENT image than whatever this chat's active branch
currently ends on (an earlier upload, an earlier result, retrying a flawed edit from its
own untouched source), tell them, in your reply, to edit/regenerate their message at the
point where that image is the active one and continue the conversation from there --
Open WebUI's own branching handles this correctly and automatically; do not try to work
around it by guessing at a file id or URL yourself.

Do NOT use generate_image for this. If the user has uploaded a photo, referenced "this
image"/"the photo"/an attachment, or is asking to modify a previously generated image in
this chat, that is always edit_image, never generate_image -- regardless of how much of the
image the request changes. generate_image is only for creating something new from a text
description with no existing image involved at all. ONE exception: a "reimagine"/"imagine"
request (see generate_image's own REIMAGINE caution) -- that's intentionally routed to
generate_image even though an existing image is involved, because the whole point there is
a from-scratch regeneration from a written description with no image conditioning at all.
Disambiguate by the trigger word itself here, unlike most other routing decisions in this
file: if the request specifically uses "reimagine"/"imagine" -- whether or not it names a
substitution -- that's REIMAGINE/generate_image (a named substitution gets spliced into a
faithful description of the photo; no substitution named means describe the photo as
faithfully as possible with nothing invented -- see generate_image's REIMAGINE caution for
the exact split between those two). Any OTHER vague phrasing that doesn't use
"reimagine"/"imagine" and names no specific substitution -- "give me a variant"/"another
take"/"different version" -- is the VARIANT caution below instead (recognizably the same
photo, just fresh details) -- default to VARIANT when genuinely unclear and the request
didn't say "reimagine"/"imagine", since it's the less drastic result.

ONE call per turn. Every edit_image call in a single turn re-edits the ORIGINAL image --
edits do not chain until your turn ends -- and only the LAST result stays visible to the
user; earlier ones are silently discarded. So: make exactly one call, then reply to the
user with what you get. If the result is imperfect, say so in your reply and let them ask
for another pass (that next turn's edit correctly builds on this result). Do NOT retry
edit_image in the same turn -- not to fix a weak result, not to reword target_region after
a rejection, not to cover a second object. A second same-turn call is refused with an
error.

There are two distinct pipelines here, chosen automatically by whether target_region is
given. Decision rule: does the edit need to reach past the specific object or area you'd
name in target_region? If yes -- relighting, weather, color grade, pose changes, or adding
something that should cast light/reflections/shadow across the rest of the scene -- leave
target_region empty. If no -- a request that names or clearly implies one specific object
or area, and nothing else should move -- provide target_region.

Caution: "add [thing] to [place]" phrasing does NOT by itself mean target_region should be
empty, even though the empty-target_region prompt template below also uses "Add [new item]
to [location/position]" wording. Judge by whether [thing] needs to visually affect anything
outside [place] (a light source, weather, smoke) vs. being a self-contained object that can
sit inside [place] untouched (a hat on a person, a sign on a wall, a boat on a lake). The
former leans global; the latter should get target_region=[place].

Further caution specific to ADDING large/prominent new content (fireworks, a bonfire, a
crowd, anything that isn't already present in the source image in some form): even when the
light-interaction argument above points toward global, whole-image mode may still fail to
produce the thing at all, not just drift less precisely. edit_strength defaults conservative
(0.35) specifically to prevent drift on unrelated content, but that same conservatism limits
how much genuinely new structure the sampler is free to introduce -- confirmed in practice:
"add fireworks in the background" at default edit_strength produced no fireworks whatsoever,
not a subtle or imprecise version of them. For a large new addition, prefer target_region on
the specific area it belongs in (masked edits run at full denoise within the mask, so they
don't have this ceiling) even if the effect would ideally extend further than the mask --
that's a real but smaller tradeoff than the edit not happening at all. Only use global mode
for a large addition if the user needs the light/atmosphere interaction badly enough to be
worth manually raising edit_strength (0.6+) and accepting more drift as a result.

Caution specific to requests for a VARIANT of the current image -- trigger phrases like "give
me a variant of this photo", "another take on this", "a different version of this same
photo", "I like everything about this but the face", "regenerate this like a fresh photo
from the same prompt", "show me some alternatives of this exact photo", "refine this
photo"/"refine this image"/"refine it" (with no further specifics on what to change) --
where the user names NO new style/season/weather/subject change at all, just wants a fresh
reinterpretation of the SAME photo: this is edit_image (target_region empty), never
stylize_image and never generate_image -- stylize_image's whole menu
(style/season/time_of_day/weather) is for applying something NEW, which is the opposite of
what a bare "variant"/"refine" request is asking for, and generate_image has no
source-image conditioning at all. "Refine" specifically used to be its own dedicated tool;
it no longer exists separately -- treat "refine" as a VARIANT trigger word exactly like
"variant" itself, not as a request needing clarification about what to refine.

CRITICAL -- do not ask the user any clarifying question for this request type (what style,
what mood, what colors, etc.) -- confirmed in practice this is a real failure mode: asking
"what kind of transformation would you like?" and listing stylize_image's menu options is
wrong here, since the user explicitly did NOT ask for something new, and every one of those
menu items (cinematic, oil painting, sepia, golden hour, ...) is itself a new-style change,
not a "same-style variant." There is nothing to ask -- you can already see the image, so
write the prompt yourself, right now, from what's actually in it: describe the composition,
subjects, setting, lighting, palette, and mood the photo ALREADY has (not a new one), the
same way you'd describe it to someone who can't see it, then call edit_image immediately
with target_region empty, that self-written prompt, and edit_strength raised to ~0.5 (see
below) -- no back-and-forth needed, this is a one-shot action.

CRITICAL -- call edit_image exactly ONCE per user request, then stop and show the result.
Confirmed in practice this is a real failure mode: given one "refine this photo" request,
the model called edit_image again on its own just-produced output, then again on THAT
output, repeatedly, chaining 8 edits deep in a single turn with no further input from the
user -- each call's own random reseed makes its result look "different again," which is
not a signal to keep going. One call fully satisfies a "refine"/"variant" request by
itself; there is no quality threshold to chase and no reason a single edit_image result
would ever need a second pass unless the user explicitly asks for another round after
seeing the first one. Do not call this tool again on a result you yourself just produced
in the same turn.

Mechanism and tuning, once you're calling edit_image for this: prompt describing the same
overall look/mood/style already present (not a new style -- just describe what's already
there) plus edit_strength raised well above its 0.35 default is what actually produces a
variant. This works because whole-image mode is standard img2img: at low edit_strength the
source's own encoded structure dominates the sampler's starting point and the result stays
close to pixel-identical; raising edit_strength gives the sampler enough injected noise to
reinterpret fine subject-level detail (faces, an animal's exact markings, a garment's exact
pattern) freshly each generation while the broad composition, palette, lighting, and subject
types stay recognizable -- genuinely different in the details, unmistakably the same photo
in feel. Confirmed empirically across a full sweep on a real photo, same seed/prompt
throughout: 0.35-0.4 is barely distinguishable from the source (not useful for this -- that's
the normal "preserve almost everything" behavior this default is tuned for); 0.45-0.65 is the
useful range, producing a clearly different face/pattern/markings while staying obviously the
same composition and subject; 0.7 is past a cliff -- confirmed producing an entirely
different image (unrelated portrait composition), not a variant at all. Use
edit_strength=0.5 as the default starting point for this use case and adjust from there only
if the user asks for it to be closer to (lower) or further from (higher, staying under ~0.65)
the original -- don't ask this as a clarifying question up front either, just start at 0.5.
Leave seed at its default -1 (random) -- that randomness is what actually produces a
different variant on each call; don't fix it unless the user specifically wants to reproduce
one exact variant again later.

target_region must name exactly ONE object/area. Segmentation is built for single-object
referring expressions -- naming two objects reliably produces a wrong/blended mask
spanning neither object correctly (confirmed in practice: "blue graduation gown and
mortarboard cap" only picked up part of the cap, and a later case naming a clothing item
plus a background object produced a mask straddling both incorrectly, with a visible seam
along the wrong boundary). If a request has two named subjects to change (e.g. "make the
gown and cap blue", two people's shirts, a garment plus a background element, etc.), do
NOT call this tool twice in the same turn to cover both -- this tool always auto-detects
its source image from the chat's own history (see above), and neither call's result is
committed to that history until the turn actually ends, so a second same-turn call
resolves to the exact same source as the first rather than building on it, and only one
of the two resulting images reliably ends up visible to the user afterward -- a
second same-turn call risks silently discarding one of the edits. Instead:
first check whether both named objects belong to one coherent subject that can be named as a
single region instead (e.g. "the gown and cap" -> "the graduate"; "her shirt and pants" ->
"her outfit" or "the woman") -- if so, use that single unified target_region in one call, no
chaining needed. Only if the two objects are NOT part of one nameable subject should you
stop and ask the user, in your reply, which one to do now -- the other needs a separate
follow-up request after they see this result, not a second call you make yourself right now.
Any target_region containing a conjunction ("and", ",", "&", "/", "or") is rejected up front
with an error (which repeats this same guidance) rather than risking another bad multi-object
segmentation. This also catches offering two alternate words for one region (e.g. "the
abdomen/torso area"), which is the same ambiguity problem in practice: Florence-2 gets two
candidate targets instead of one and the resulting mask reliably lands on neither correctly.
This is deliberately broad: it will also reject a genuinely single-object description that
happens to contain "and"/"," (e.g. "the black and white jacket") -- rephrase without that
word in that case (e.g. "the black-and-white jacket" or "the jacket").

target_region wording -- name the object by its PLAIN VISUAL CATEGORY, the everyday
common noun a person would use pointing at it, NOT a brand, make/model, species breed, or
proper name. The segmenter matches on what the thing looks like, so a specific name it
doesn't recognise (or that the pixels don't clearly match) produces a patchy mask that
covers only the most on-concept part -- e.g. "the red Lamborghini" on a car that doesn't
read as a Lamborghini masked the sharp front but not the rounded rear, so a swap converted
the front and left the rear as the original car. Use the category:
  "the red Lamborghini" / "the cherry-red supercar"  -> "the red car"  (or "the sports car")
  "the Chrysler Building"                             -> "the tall building" / "the skyscraper"
  "Rex" / "the user's golden retriever"              -> "the dog"
  "Sarah" / "the bride"                               -> "the woman"  ("the woman in white" ok)
  "my iPhone 15 Pro"                                  -> "the phone"
Colour, position and relation qualifiers are fine and help disambiguate ("the red car",
"the woman on the left", "the dog nearest the camera") -- it's only the identity-level name
(brand / model / person's name / specific breed) that hurts. Keep the specific identity for
the `prompt` (the description of the result), not for `target_region`.

prompt wording for a masked edit -- write it as a DESCRIPTION of the finished image, a
caption of what should be there, NOT an instruction. The masked pipeline feeds `prompt`
straight into a caption-style image model with no rewriting: instruction phrasing leaves
the original subject in play and fights the change.
  BAD:  "Replace the red Lamborghini with a blue Audi R8, keep the background"
  GOOD: "a metallic blue Audi R8 sports car, parked in the same spot, same angle, same
         lighting and reflections"
Do not mention the original object at all in the prompt -- describe only the desired
result. (A leading "replace X with"/"change X to"/"turn X into"/"make it" is stripped
server-side as a safety net, but write it right and don't rely on that.)

Caution: if target_region names a diffuse area rather than a discrete, nameable object
("the background", "everything behind the family", "the surroundings") -- segmentation is
built to find one concrete object/area, not a loose category, and will often return a
narrow, wrong, or oddly-shaped region for a request like that (confirmed in practice: "the
background behind the woman and the dog" segmented only the small sliver of canopy
immediately behind them, not the broader background actually intended). When the edit is
conceptually "everything except X" and X is the discrete, reliably-detectable thing (the
people, the dog, a named object) rather than the diffuse area itself, set target_region=X
and invert_mask=True instead of trying to name the diffuse area directly.

Examples:
  "make the sky more dramatic and add clouds"        -> target_region omitted
  "change her jacket to blue"                        -> target_region="jacket"
  "add fog rolling through the whole scene"           -> target_region omitted
  "remove the person standing on the left"            -> target_region="person on the left"
  "give it a warmer, golden-hour feel"                -> target_region omitted
  "make the gown and cap blue"                        -> target_region="the graduate" (one unified region covering both -- NOT two chained calls; target_region="the gown and cap" returns an error)
  "make her shirt and her friend's hat both red"       -> two unrelated subjects, not one nameable region -- ask the user which to do first rather than calling this tool twice
  "add fireworks lighting up the sky, glow on everyone below" -> target_region="the sky behind the family" (large new addition -- global mode may add nothing at all at safe edit_strength; see caution above)
  "add a party hat on the little girl"                -> target_region="the little girl" (self-contained object)
  "put a cowboy hat on the man riding the horse"      -> target_region="the man riding the horse", NOT "his head" (see headwear caution below)
  "put a sailboat on the lake in the background"      -> target_region="the lake in the background" (self-contained object)
  "add a six-pack / abs definition"                   -> target_region="his bare torso" (ONE concrete word for the body part -- not "abdomen/torso area", which reads as two alternate words plus a vague "area" suffix and gets rejected as ambiguous)
  "add fireworks in the background/canopy behind everyone" -> target_region="the people and the dog", invert_mask=True (diffuse area -> invert a reliable detection instead)
  "replace this person with a different person, same pose/style" -> target_region="<current description of that person>", preserve_scene_context=False (full identity swap -- override the default here)
  "change this person's shirt to blue, keep everything else the same" -> target_region="<current description of that person>" (surface attribute edit -- leave preserve_scene_context at its default True)
  "reimagine this photo but with a bay horse instead"  -> NOT this tool -- generate_image's REIMAGINE, see that tool's docstring

Caution specific to headwear/helmets/masks/hoods or anything else added ON TOP OF a body
part rather than replacing a part of it (a cowboy hat, a crown, a helmet, a halo): only
pixels INSIDE the (grown/blurred) mask can be painted, and a request phrased around the
body part itself -- "a hat ON HIS HEAD" -- tempts target_region="his head", which
segments the tight visible head/hair silhouette with only a small margin. That silhouette
does not include the empty space above the head where a hat's crown actually needs to be
drawn, so the edit has nowhere to render it and produces no visible change at all -- not a
misplaced or undersized hat, no hat (confirmed in practice: "make the man wear a cowboy
hat" with target_region on the head/person didn't add one). Set target_region to the whole
subject the item will sit on (the person, not their head) so the mask includes the
surrounding headroom -- same reasoning as the party-hat example above, just easy to miss
when the request's own wording names the body part instead of the person.

Caution specific to bare-skin/body-region edits (abs, muscle tone, tan lines, tattoos on
skin, etc.): unlike clothing, bare skin has no hard visual boundary for segmentation to
lock onto, so these are inherently harder to mask accurately than an object with a clear
edge. Use one plain word for the body part ("torso", "stomach", "chest", "arm") rather
than hedging with alternates or a vague "area"/"region" suffix -- that hedging is exactly
what gets rejected as ambiguous, and even past that check, vaguer phrasing tends to segment
worse. Add a position/appearance anchor if the first attempt's mask looks off, e.g. "his
bare stomach below the chest".

Caution: avoid describing a subject by a clothing/object color that could also plausibly
match something else in the scene (foliage, sky, grass, another person's clothing) -- prefer
position or relationship instead ("the man standing next to the woman in pink", "the person
on the left") over color ("the man in the green shirt"), especially outdoors or in scenes with
multiple people. Confirmed in practice: "the man in the green shirt" against a background of
dense green trees produced a small fragmented mask, while a positional description of the
identical subject in the identical image produced a clean, complete, correctly-bounded mask.

Caution: describe target_region as a single coherent reference to the subject itself (e.g.
"the man standing next to the woman in pink"), never as a list of body parts (e.g. "the
body, arms, and legs of the man") even for an edit that only visually affects part of them.
Segmentation is trained on single-object referring expressions; an enumerated part-list is a
mismatched query format for it and reliably produces an undershot/fragmented mask instead of
the whole subject.

Caution: preserve_scene_context defaults to True, because most target_region edits are surface
attribute changes (color, clothing, an accessory, an expression) rather than identity-level
changes -- and attribute changes need it True. This pipeline conditions the masked edit on a
second pass built from the whole original image by default, which helps ordinary attribute
edits blend with their surroundings and keeps the model anchored to the rest of the scene --
dropping that anchor on a non-identity edit has produced corrupted results in practice (a
plain color-only edit rendered the subject backwards with hair drawn over the face). Set
preserve_scene_context=False explicitly ONLY for genuine identity-level swaps -- for those,
the same full-image pass conditions the model on the very person/thing you're changing away
from and can produce an incomplete result.

Caution: adding NEW readable lettering onto an existing surface (a name on a sail, text on a
sign or shirt) via target_region is inherently unreliable on this pipeline, on every model --
confirmed in practice across multiple structural attempts at improving it. This graph has no
mechanism to warp text to the surface's perspective/curvature/lighting, so the result is a
coin-flip between illegible/garbled letters and legible text that looks pasted flat on top
rather than part of the fabric. This is a pipeline limitation, not fixable by retrying with
different edit parameters. Tell the user this specific kind of edit (new lettering added
onto a photographed surface) is unreliable here, and that a retry with a fresh seed
sometimes helps but isn't guaranteed. If the user mainly wants clean rendered text and
doesn't need to preserve the rest of the source photo, a plain generate_image with the
wording written into the prompt is a better bet than an edit_image masked edit.

:param prompt: Description of the transformation to apply, e.g. 'make the sky red at sunset'.
    REQUIRED -- there is no chat-history-derived fallback here. When target_region is empty,
    always state that everything not part of the change should stay entirely unchanged (pose,
    expression, background, lighting, other objects) -- that pipeline has no masking, so
    unrelated parts of the image will otherwise drift. Use one of:
    - Replacing: "Replace [item] with [new item]. Keep everything else -- pose, expression,
      background, lighting, and other objects -- entirely unchanged."
    - Adding: "Add [new item] to [location/position]. Keep everything else -- pose, expression,
      background, lighting, and other objects -- entirely unchanged."
    - Removing: "Remove [item] from the image, filling in naturally where it was. Keep
      everything else -- pose, expression, background, lighting, and other objects -- entirely
      unchanged."
    When target_region is given, that "keep everything else unchanged" framing is unnecessary
    (the mask already guarantees it) -- just describe the change itself plainly.
:param target_region: Plain-language description of ONE object or area to confine the edit
    to, e.g. 'the red car', 'the woman in the gray sweater', 'the sign in the background'.
    Leave empty for a whole-image edit. See the decision rule above.
    HARD RULE -- target_region must name exactly ONE thing. Any "and" / "," / "&" / "/" /
    "or" in the text is REJECTED outright (the segmenter blends or misses with more than one
    target). There is no multi-region edit. If a request needs two regions changed: name a
    single subject that covers both if one exists ('the cap and gown' -> 'the graduate',
    'the people and the dog' -> 'the family'); otherwise make ONE of the changes and tell
    the user in your reply that the other needs a separate follow-up request. Do NOT reword
    target_region and retry repeatedly to get past a rejection.
    Name the object by its plain visual category, not a brand / model / proper name (see the
    "target_region wording" note above) -- 'the red car', not 'the red Lamborghini'.
:param aspect_ratio: 'auto' (default, preserves the source image's aspect ratio) or one of 'square', 'landscape', 'portrait', 'widescreen', 'tall', 'ultrawide', 'ultratall'. Ignored if width and height are both given.
:param width: Explicit width in pixels, multiple of 16, between 256 and 2048. Requires height. Overrides aspect_ratio.
:param height: Explicit height in pixels, multiple of 16, between 256 and 2048. Requires width. Overrides aspect_ratio.
:param seed: Fixed seed for reproducibility, or -1 for a random seed.
:param edit_strength: Only used when target_region is empty (ignored otherwise). How strongly
    the edit applies vs. how much of the source is preserved, 0.05-1.0. Default 0.35. Lower
    preserves more of the original but the edit may not come through; higher applies the edit
    more strongly but more of the rest of the image can drift. For a "variant of this image"
    request specifically (see the caution above), 0.45-0.65 is the confirmed useful range --
    0.35-0.4 stays too close to the source to read as a variant, 0.7+ can jump to an unrelated
    image entirely.
:param color_correct_strength: Only used when target_region is given (ignored otherwise), 0-1,
    default 0.95. Flux.2 tends to shift the whole frame's color/saturation slightly on every
    generation regardless of masking; this corrects the output's colors back to match the
    original outside the edited region. Set to 0 if this specific edit is meant to shift the
    overall color/mood on purpose.
:param invert_mask: Only used when target_region is given (ignored otherwise). When True, the
    edit applies to everywhere EXCEPT target_region instead of to target_region itself. Use
    this when the edit's real target is a diffuse area (background, surroundings) that's hard
    to segment directly -- set target_region to the reliable, discrete thing that should stay
    unchanged and invert_mask=True, rather than trying to name the diffuse area itself.
:param preserve_scene_context: Only used when target_region is given (ignored otherwise),
    default True. Set False explicitly only for a genuine identity-level swap (replacing a
    person or animal with a visually different one).
:param edge_softness: Only used when target_region is given (ignored otherwise), default
    28 (pixels). Controls two related things together: (1) how far past the raw
    segmentation boundary the sampler is actually allowed to generate new content at all
    (the paint extent), and (2) the radius of a real Gaussian blur applied to the mask
    before the final blend, so the edit fades into the surrounding image following the
    object's actual silhouette rather than cutting off sharply. Higher values both blend
    more smoothly AND give the sampler more room to draw structure that extends past the
    segmented subject's existing outline -- raise this for additions that need real
    clearance beyond the subject's current silhouette (a hat's crown above a head, wings, a
    raised prop): too little room here produces a faint, barely-visible result with the
    original showing through where the addition should be, NOT a wrong-shaped or missing
    addition. Lower values give a crisper edge and a tighter paint extent, at higher risk of
    a visible seam on high-contrast or glow/bloom-heavy content. 0 skips blurring entirely
    for a hard, unfeathered edge at the base (unexpanded) paint extent; values are capped at
    31 internally (ComfyUI's blur node maximum). The blur can never bleed the edit past the
    (softness-expanded) paint margin into unrelated background (it's clamped against a
    dilated version of the paint mask before use).
:param rewrite_prompt: 'auto' (default) / 'off' / 'on' -- see generate_image. Only applies to whole-image
    edits; ignored for masked edits.

There is NO model parameter. Editing always runs on the same pipeline (flux2-klein on the
iGPU), in every mode. Ignore any user request to "use model X" for an edit -- just call
the tool normally. The masked-region caution above is a pipeline limitation, not something
a different model would fix.
