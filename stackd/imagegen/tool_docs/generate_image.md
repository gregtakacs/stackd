Generate a new image from a text prompt using a local ComfyUI Flux.2 pipeline.
Use this whenever the user wants an image created from a description. There is no
negative-prompt input -- there is no negative_prompt parameter.
There is no image parameter at all -- that's intentional, not a limitation to work around
(see the REIMAGINE caution below for the one case where an existing image is still relevant
despite that).

REIMAGINE: a "reimagine this photo" request -- trigger phrases like "reimagine this photo,
but with a bay horse instead", "same scene but three men instead of women", "reimagine this
with X", or a bare "reimagine this photo"/"imagine this" with no further instructions at
all -- where the user wants the SAME photo regenerated from a text description rather than
a recognizable edit of the original pixels. This belongs here, not edit_image or
stylize_image, even though an existing photo is involved -- see edit_image's and
stylize_image's own "Do NOT use generate_image" cautions for the exception carved out for
this case. The mechanism: you can already see the referenced photo in this conversation, so
DON'T pass an image anywhere (this tool has no image parameter to pass one to anyway) --
instead, write prompt yourself, right now.

CRITICAL -- if this is a FOLLOW-UP reimagine (you or another turn in this same
conversation already called generate_image for REIMAGINE earlier), base your new
description on YOUR OWN MOST RECENT reimagine prompt from this conversation's tool-call
history -- not a fresh look at the original photo. There is no image parameter here for
this tool (or this server) to re-resolve "the right photo" from the way edit_image's own
auto-detect can, and by a later turn you may not have fresh visual access to
the original attachment at all -- your own prior prompt text is the actual record of
what's been established so far, INCLUDING any substitutions already applied by earlier
reimagine turns. Apply the new request as a further change layered onto that prompt, the
same way edit_image/stylize_image chain onto their own most recent result instead of
reverting to the original upload. Re-describing from the original photo on a follow-up
turn is wrong even if you can still technically see it -- confirmed in practice this
silently discards every substitution applied by earlier turns in the same reimagine
thread, the exact same "reverted to square one" failure mode edit_image/stylize_image
had before their own chaining fix. Only start fresh from the original photo on the FIRST
reimagine request in a conversation, never a follow-up one.

There are two distinct cases, both routed here, that differ only in what goes into that
description (built from the original photo on a first request, or from your own prior
reimagine prompt on a follow-up one, per the above):

1. EXPLICIT substitution named (a different species/breed, a different number/gender of
   subjects, swapping what something fundamentally IS): write a maximally detailed
   description of everything actually in the photo (composition, subjects, setting,
   lighting, colors -- same novelist Subject -> Setting -> Details -> Light style
   documented below), then splice the user's requested substitution naturally into that
   description in place of whatever it's replacing (their "bay horse" replaces your
   description of the original horse; "three men" replaces your description of however
   many women were there), keeping everything else -- pose, setting, lighting, framing --
   as described. Make no OTHER alterations of your own beyond that one requested
   substitution.

   When the substitution is a shared trait across MULTIPLE subjects at once (an
   ethnicity/race, a species applied to several animals, a uniform hair or eye color
   across a group), pair the category word with concrete VISUAL descriptors -- skin
   tone, hair color/texture, etc. -- repeated for EACH individual subject, not stated
   once for the group or left as the demographic/category label alone. Confirmed in
   practice this materially changes the result: "four African American people" alone
   produced a visibly mixed-fidelity result (2 of 4 subjects clearly matched, 2 didn't);
   adding "deep brown skin, black coily hair" etc. to each individual's own description
   produced consistent results across all four. This pipeline's distilled 4-step sampler
   has real limits on multi-subject attribute binding -- a category word per subject
   isn't enough on its own, concrete per-subject visual language is what actually binds
   reliably.

2. BARE "reimagine"/"imagine" with no substitution named: write prompt as a maximally
   faithful, factual description of exactly what's in the photo -- same number of
   subjects, same species, same genders, same setting, same everything you can actually
   observe -- and do NOT invent your own substitutions on top of it. Confirmed in practice
   this is a real failure mode: given no instructions, the model has invented unrequested
   changes (e.g. replacing people with dogs) -- that is always wrong here. There is no
   license to introduce anything the user didn't ask for just because the trigger word is
   "reimagine"/"imagine"; any difference from the source should come only from the
   mechanism itself (see below), never a deliberate content swap you added.

Each human subject's body type/build (height, weight, build -- e.g. "tall and lean", "short
and stocky", "heavyset", "petite", "broad-shouldered and muscular") needs the same explicit,
per-subject treatment as ethnicity above, in BOTH cases (it's part of faithfully describing
who's already in the photo, whether or not the requested substitution touches it).
Confirmed in practice this is a real failure mode: generated subjects default to an
average/"normal" build whenever build isn't spelled out in the prompt, even when the actual
photo clearly shows something different (visibly heavier, thinner, taller, more muscular,
etc.) -- the same silent-default problem the ethnicity/race guidance above already fixes for
that trait. Look at each subject's actual build in the photo and describe it plainly, the
same way you'd describe their hair color or clothing -- don't leave it unstated and don't
default to "normal"/average just because the user's request didn't specifically mention it.

Either way, call this tool immediately with that prompt once written -- this is a one-shot
action, the same way edit_image's VARIANT caution is; don't ask the user to describe the
photo for you or to clarify further, you already have everything you need by looking at it.
Leave aspect_ratio/width/height unset -- the original photo's own aspect ratio is matched
automatically (see aspect_ratio's own docstring below); don't try to eyeball and set it
yourself, visually distinguishing e.g. 4:3 from 16:9 from a rendered image isn't reliable.

Both cases aim to stay AS CLOSE to the source photo as this mechanism allows -- case 1
changes only the one thing the user asked for, case 2 changes nothing on purpose at all.
Boring and close to the original is the goal, not an interesting or "wildly different"
reinterpretation. The only reason the result won't be pixel-identical is mechanical, not
intentional: unlike edit_image's whole-image path, there is no source latent feeding the
sampler at all here (pure text-to-image, denoise=1 from noise), so nothing anchors the
output to the original photo's actual pixels -- only your written description does, and a
written description, however detailed, always leaves countless specifics (exact facial
structure, exact lighting falloff, exact texture) up to fresh interpretation. Treat that as
a limitation of the mechanism to minimize through a more detailed/precise description, never
as license to add your own creative changes on top of it. If the user actually wants the
result to still look like the same photo (recognizably the same composition/subject, just a
different face or a different rendering of one detail) without asking to literally
regenerate-from-text, that is NOT this -- that's edit_image's VARIANT caution instead, which
conditions on the actual source image and stays recognizable by design.

:param prompt: Detailed positive description of the desired image. REQUIRED -- there is no
    chat-history-derived fallback here (this tool only sees the arguments you pass, not the
    conversation), so if this is left blank the call fails; write a complete prompt yourself
    every time. Write like a novelist: Subject -> Setting -> Details -> Light. Include
    technical photography terms and quality/processing language that signal a real
    photograph -- but choose ones that actually fit the specific scene, camera distance, and
    lighting you're describing, not a fixed default: a close portrait might call for
    something like "shot on Canon R5, 85mm f/1.4, shallow depth of field"; a wide outdoor
    landscape calls for something else entirely, e.g. "24mm wide-angle, deep focus, natural
    daylight"; a studio product shot might use "studio strobe, clean digital file, histogram
    equalization". These are illustrative examples of the KIND of vocabulary to reach for,
    not a checklist to insert verbatim regardless of fit -- confirmed in practice this is a
    real failure mode: REIMAGINE calls have reused this exact "Canon R5, 85mm f/1.4" /
    "histogram equalization" phrasing verbatim across photos where neither the camera/lens
    choice nor studio-processing language actually matched the scene (e.g. an outdoor
    group photo, or a wide shot, described with a narrow-portrait lens and studio-lighting
    terms that don't apply to daylight). Match the technical language to what the photo
    actually shows every time, the same way you'd match any other descriptive detail. Put
    any rendered text in quotes, e.g. a sign reading "HELLO" -- text left unquoted tends to
    render as gibberish. Be specific (e.g. "35-year-old woman with auburn hair") rather than
    vague ("beautiful", "stunning", "masterpiece"). Never use SD-style tag lists ("best
    quality, ultra detailed") or "trending on artstation" (causes stylistic drift).
    For a REIMAGINE request (see above), write this yourself from what you see in the
    referenced photo -- plus the user's requested substitution if they named one, or as a
    purely faithful description with nothing invented if they didn't.
:param aspect_ratio: One of 'square', 'landscape', 'portrait', 'widescreen', 'tall', 'ultrawide', 'ultratall',
    or leave blank (default) / pass 'auto' (an accepted alias for blank, same as edit_image/
    stylize_image's own 'auto'). Ignored if width and height are both given. Blank/'auto', for a
    REIMAGINE call this auto-matches the referenced photo's own aspect ratio (reads only its pixel
    dimensions, not its content -- see the REIMAGINE caution above); don't set this yourself for
    REIMAGINE unless the user specifically wants a DIFFERENT aspect ratio than the original photo.
    Falls back to 'square' if there's no photo to detect one from (e.g. a plain "generate an image
    of X" with nothing to match).
:param width: Explicit width in pixels, multiple of 16, between 256 and 2048. Requires height. Overrides aspect_ratio.
:param height: Explicit height in pixels, multiple of 16, between 256 and 2048. Requires width. Overrides aspect_ratio.
:param seed: Fixed seed for reproducibility, or -1 for a random seed.
:param rewrite_prompt: 'auto' (default) lets the tool decide whether to expand your prompt
    via the shared LLM. 'off' forces your prompt through verbatim -- use it when you have
    already written a full, deliberate prompt. 'on' forces expansion.

:param image_model: OPTIONAL. Leave this EMPTY. The right pipeline is chosen
    automatically for you. Set it ONLY when the user explicitly asks for a specific
    image model/pipeline. Pass your closest match to one of these names:
      - flux2-dev-turbo -- Flux.2 dev + Turbo LoRA, ~8 steps; higher quality, larger
      - flux2-klein     -- Flux.2 Klein 9B, distilled ~4 steps; faster, smaller
      - ideogram4       -- Ideogram 4; best when the image needs accurate rendered
                           TEXT / typography / signage / logos
    Match loosely: "klein" / "the fast one" -> flux2-klein; "dev" / "turbo" /
    "the good one" / "higher quality" -> flux2-dev-turbo; "ideogram" or a request
    centred on readable text in the image -> ideogram4. If your value doesn't
    resolve you get the real list back in the error -- retry with a name from it.
    Do NOT tell the user which pipeline was used -- not before a generation and not
    after -- unless they specifically ask. The one exception: if you named a
    pipeline and the result carries a note that it was swapped for a smaller one,
    briefly mention that.

The result JSON has a `timing` object (`total_s`, `generate_s`, and `rewrite_s`
when the prompt was rewritten). Do not report it unless the user asks how long it
took.
