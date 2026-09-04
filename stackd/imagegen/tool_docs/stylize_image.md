Apply one or more curated, hand-tuned adjustments to an existing image in a single
generation, using a local ComfyUI Flux.2 pipeline. This is NOT a
free-text style/transformation tool -- there is no prompt parameter, every adjustment comes
from the fixed menu below, and it cannot apply anything that isn't already in that menu.

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
currently ends on, tell them, in your reply, to edit/regenerate their message at the
point where that image is the active one and continue from there -- Open WebUI's own
branching handles this correctly and automatically; do not try to work around it by
guessing at a file id or URL yourself.

CRITICAL -- call stylize_image exactly ONCE per user request, then stop and show the
result. Confirmed in practice this is a real failure mode: given one "make it into a
cartoon" request, the model called stylize_image again on its own just-produced output,
then again on THAT output, repeatedly, chaining 16 calls deep in a single turn with no
further input from the user -- each call's own random reseed makes its result look
"different again," which is not a signal to keep going. One call fully applies the
requested adjustment(s); there is no quality threshold to chase and no reason a single
result would ever need a second pass unless the user explicitly asks for another round
after seeing the first one. Do not call this tool again on a result you yourself just
produced in the same turn -- this applies just as much to a single style/season/weather
request as it does to a bare "refine"/"variant" request routed here from edit_image.

Five independent, freely combinable adjustments -- set any subset of them (including just
one, including all five) in a single call; whichever are set get merged into one prompt and
run through one generation, not several separate calls:
  - style: one of seven base RENDERING treatments, or "" (default) for none. These are
    mutually exclusive by nature (an image can't be both an oil painting and a line
    drawing), so pick at most one.
      * "cinematic": professional studio-style photography -- dramatic rim/key lighting,
        high contrast with deep shadows, heavy background bokeh blur, sharp 85mm-lens-style
        foreground focus, ultra-realistic skin texture, moody atmosphere.
      * "cartoon": colorful Western/Pixar-style cartoon-avatar illustration -- explicitly
        NOT anime/manga (no oversized eyes, no screentone shading, no ink-brush linework) --
        clean bold outlines, semi-flat cel shading, warm friendly character design.
      * "line_art": clean black-and-white ink illustration -- bold confident outlines,
        minimal/no shading, no color anywhere including the background.
      * "oil_painting": genuine hand-painted look, explicitly NOT photorealistic -- thick
        visible impasto brushstrokes, abstracted detail, canvas texture, classical/
        impressionist portrait feel.
      * "polaroid": vintage instant-camera look -- soft hazy focus, faded warm tones,
        vignette, grain, a white photo border. The border is a framing/canvas element in
        tension with this pipeline's strong composition-preservation behavior -- it may not
        render reliably; treat as unconfirmed until tested.
      * "vintage_photo": genuinely old/aged photograph -- faded or sepia colors, film grain,
        scratches/dust, muted contrast, gentle vignette. Does NOT reliably produce strict
        monochrome sepia specifically -- if the user asks for "sepia" by name, use
        color_treatment="sepia" instead, not this style.
      * "manga": black-and-white Japanese manga illustration -- expressive large manga-style
        eyes, dynamic ink linework, screentone dot-pattern shading. Deliberately the mirror
        image of "cartoon": use "manga" when the user actually wants that anime/manga look,
        use "cartoon" when they want a Western/Pixar-style look and explicitly do NOT want
        anime/manga conventions.
  - color_treatment: a color/tone modifier, separate from style because it's meant to layer
    on top of ANY of them (or none): "cartoon" + color_treatment="black_and_white" gives a
    black-and-white cartoon, "oil_painting" + color_treatment="sepia" gives a sepia-toned
    painting, etc. One of "sepia", "black_and_white", "colorize", "vivid", or "" (default).
      * "sepia": strict monochrome brown-toned conversion. Use whenever the user says
        "sepia" explicitly, rather than "vintage_photo" (which only sometimes produces
        sepia toning).
      * "black_and_white": strict monochrome grayscale conversion, no color cast of any kind.
      * "colorize": adds natural, plausible color -- the opposite of sepia/black_and_white.
        Use for "colorize this old/black-and-white photo".
      * "vivid": boosts existing color saturation/vibrancy, without adding color to a
        monochrome source the way "colorize" does.
  - season: changes the apparent season -- adjusts environmental elements only (foliage,
    ground cover, sky, ambient light). One of "spring", "summer", "autumn", "winter", or ""
    for no change.
  - time_of_day: changes the apparent time of day/lighting -- adjusts ambient light, sky,
    and shadows only. One of "dawn", "morning", "midday", "golden_hour", "sunset", "night",
    or "" for no change.
  - weather: changes the apparent weather -- adjusts sky, precipitation, and ground surface
    only. One of "clear", "overcast", "rainy", "downpour", "foggy", "stormy", "snowy", or ""
    for no change. "rainy" is a calm, aftermath-of-rain look; "downpour" is heavy, actively-
    falling, dramatic rain -- pick "downpour" when the user wants intense/stormy rain.
All five leave composition, camera angle, subject positions, pose, and facial features
exactly as they are in the source image -- only the specific things named above change.
At least one of the five must be set (non-empty) -- calling with all five left empty is an
error, since there would be nothing to actually do.

Example: a "cinematic winter sunset" version of a photo is ONE call with style="cinematic",
season="winter", time_of_day="sunset" -- not three separate calls. Combining like this in
one call is preferred over calling this tool multiple times in sequence for compound
requests: each call is a full regenerate conditioned on whatever image it's given, so
chaining several calls means each pass builds on the previous pass's output rather than on
the original photo, and small drifts can compound across passes.

If the user describes a specific custom transformation in their own words that isn't covered
by combining the adjustments above (e.g. "make it look like a 1970s film photo", "add
dramatic red lighting"), that request does not belong here -- use edit_image's whole-image
path (target_region empty) instead.

Caution -- a bare "give me a variant of this photo" / "another take on this" / "a different
version of this same photo" request, where the user names NO new style/season/time_of_day/
weather at all, does NOT belong here even though it sounds like a stylize-ish request --
it's asking for the OPPOSITE of what every option on this tool's menu does. Every one of
style/color_treatment/season/time_of_day/weather applies something NEW; a bare "variant"
request wants the SAME look, just a fresh reinterpretation of specific details (a different
face, different animal markings, a different window/pattern) while everything else -- mood,
palette, composition -- stays as-is. Do not guess a style/season/weather for a request like
this, and do not ask the user to pick one either. Route it to edit_image instead
(target_region empty, edit_strength raised to ~0.5) -- see the detailed "VARIANT" caution in
edit_image's own docstring for the mechanism and exact tuning.

Do NOT use generate_image for this -- see edit_image's identical caution, it applies here
too: if there's an existing image involved, this or edit_image is correct, never
generate_image. ONE exception: a "reimagine"/"imagine" request -- whether or not it names a
content-level substitution (a different species/breed, a different number or gender of
subjects) -- see generate_image's own REIMAGINE caution -- IS generate_image, not this tool
or edit_image, since that's a deliberate from-scratch regeneration with no image
conditioning at all, not a style/look adjustment.

This pipeline holds composition/pose/subject identity more reliably than edit_image's
whole-image path under a large visual transformation, because it conditions the model on the
source image's own latent via ReferenceLatent rather than trading identity preservation
against a single denoise value the way img2img does.

:param style: One of "cinematic", "cartoon", "line_art", "oil_painting", "polaroid",
    "vintage_photo", "manga" to apply that base rendering treatment, or "" (default) for
    none. At most one style, since these are mutually exclusive rendering approaches.
:param color_treatment: One of "sepia", "black_and_white", "colorize", "vivid", or ""
    (default) for none. Combinable with style and all other parameters in the same call.
:param season: One of "spring", "summer", "autumn", "winter", or "" (default) for no
    season change.
:param time_of_day: One of "dawn", "morning", "midday", "golden_hour", "sunset", "night",
    or "" (default) for no time-of-day change.
:param weather: One of "clear", "overcast", "rainy", "downpour", "foggy", "stormy", "snowy",
    or "" (default) for no weather change.
:param upscale_by: Multiplier applied to the source image's own resolution before stylizing
    (not an absolute width/height). Default 1.0 (no upscale). Values above 1.0 increase
    output resolution and detail at the cost of more VRAM/time.
:param seed: Fixed seed for reproducibility, or -1 for a random seed.
:param image_model: OPTIONAL. Leave this EMPTY -- the pipeline is chosen for you.
    Set it ONLY when the user explicitly asks for a specific image model. Closest
    match to one of:
      - flux2-dev-turbo -- Flux.2 dev + Turbo LoRA; higher quality, larger
      - flux2-klein     -- Flux.2 Klein 9B, distilled; faster, smaller
    "klein" / "the fast one" -> flux2-klein; "dev" / "turbo" -> flux2-dev-turbo. A
    bad value returns the real list -- retry with one from it. Do NOT tell the user
    which pipeline ran, before or after, unless they ask -- except to briefly note
    it if you named one and the result says it was swapped for a smaller one.
    Composition is always held (denoise-1 + ReferenceLatent); only the requested
    treatment changes.

The result JSON has a `timing` object (`total_s`, `generate_s`). Do not report it
unless the user asks how long it took.
