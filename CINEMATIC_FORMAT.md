# Cinematic storyboard format for LOTUS

Companion to `CLAUDE_FORMAT.md`. That one covers flat image packs (slides,
stories, carousels). This one covers **a film**: one topic in, a complete
storyboard out, then Google Flow clips generated from the boards you
approved.

The pipeline it feeds:

```
topic → Claude writes this document
      → LOTUS renders ASSETS   (NB2 stills — characters, locations, props)
      → LOTUS renders KEYFRAMES(NB2 stills — one per act, using the assets)
      → you approve boards in the dashboard
      → LOTUS sends each ACT PROMPT + its approved keyframe to Google Flow
        (image→video, so the clip matches the board instead of Flow
         reinventing the shot)
```

Assets render first on purpose. Nano Banana has no memory between calls, so
the only way act 7 looks like act 1 is if both were written to describe the
same asset sheet. That's what the ASSET blocks are for.

---

## The prompt to paste into Claude.ai

```
CINEMATIC HOUSE FORMAT — FOLLOW EXACTLY (Lotus parses this deterministically)

I'll give you a topic. Produce a complete cinematic storyboard as ONE
markdown file in the structure below. Heading levels MUST match exactly.

# FILM — <title>

## TREATMENT
<logline, tone, audience, runtime. Prose. No code blocks here.>

## STYLE BIBLE
<film stock / lens / palette / lighting / grade. One paragraph. This text
gets appended to every NB2 prompt, so make it concrete and reusable.>

## ASSET 1 — <character name>
### NB2 PROMPT
` ` `
<full-body character sheet, neutral pose, plain seamless backdrop, even
lighting. Describe face, build, wardrobe, distinguishing marks in specific
nouns. This image becomes the reference for every act the character is in.>
` ` `

## ASSET 2 — <location name>
### NB2 PROMPT
` ` `
<establishing plate of the location, no characters present>
` ` `

## ACT 1 — <beat title>
### SHOT
<one line, plain text: shot size, lens, camera position. Not a code block.>
### NB2 PROMPT
` ` `
<the KEYFRAME still for this act — the single frame that represents the
shot. Re-describe every asset that appears, in the same nouns used in its
ASSET block. Bake in the STYLE BIBLE language. This is a still: describe
the frozen moment, not the movement.>
` ` `
### ACT PROMPT
` ` `
<the MOTION for this shot, written for Google Flow / Veo. Describe what
moves and how the camera moves — subject action, camera push/pan/tilt,
speed, what the shot ends on. Do NOT re-describe the visual design; the
keyframe image already carries it. 1-3 sentences.>
` ` `

## ACT 2 — <beat title>
### SHOT
<...>
### NB2 PROMPT
` ` `
<keyframe>
` ` `
### ACT PROMPT
` ` `
<motion>
` ` `

RULES
1. Exactly one `### NB2 PROMPT` and one `### ACT PROMPT` per ACT, each a
   fenced code block. An act with no ACT PROMPT renders as a still only.
2. ASSET blocks have an NB2 PROMPT only — never an ACT PROMPT. Assets are
   reference sheets, not shots.
3. Number acts sequentially from 1. Acts are the edit order.
4. NEVER put a renderable prompt in bold text, bullets, tables, or quotes.
   Only fenced code blocks under a `### … PROMPT` heading are rendered.
5. Non-rendered material (TREATMENT, STYLE BIBLE, SHOT, DIALOGUE, SOUND,
   NOTES) goes under headings that do NOT contain the word "PROMPT".
6. Keep every ACT PROMPT under ~40 words. Veo degrades on long motion
   prompts — it starts ignoring the source image.
7. Output the entire file in a single message.

Ask me for the topic if I haven't given it yet.
```

(Replace each ` ` ` above with three consecutive backticks — spaced out here
only because this file is itself fenced.)

---

## How LOTUS reads it

| Heading path | kind | Rendered by | Costs |
|---|---|---|---|
| `## ASSET n` · `### NB2 PROMPT` | `asset` | image chain (`_gen_image`) | free |
| `## ACT n` · `### NB2 PROMPT` | `image` | image chain (`_gen_image`) | free |
| `## ACT n` · `### ACT PROMPT` | `motion` | `_gen_video` image→video via Flow | **~7+ credits/clip** |
| anything without `PROMPT` in the path | — | skipped | — |

`prompts_parser.extract_candidates()` composes the full heading hierarchy
into `title` (joined by ` · `), which is what lets a single H3 be resolved
back to its act number and kind.

**Credits are the reason the review gate matters here.** Stills are free, so
regenerating a board costs nothing. Every act prompt you approve spends real
Flow credits, and a 12-act film is ~85 credits. Approve the boards first,
look at them as a sequence, and only then approve motion.

---

## Aspect and duration

Flow clip defaults come from `~/.lotus_config.json` under `video`
(`video.default_aspect`, `video.default_duration`), NOT from this document —
`_pipeline_generate_approved_video` reads them per run. Cinematic work
usually wants `16:9`; the LOTUS default is `9:16` because the pipeline grew
out of Reels. Set it before the first run:

```json
"video": { "pipeline": "flow", "default_aspect": "16:9", "default_duration": "8s" }
```

Duration is per clip. Veo caps a single generation at 8s, so an act that
needs longer is two acts sharing a keyframe.

---

## Joining the clips

LOTUS renders individual MP4s to `<pipeline>/Clips/Clip{N}.mp4` in act
order. The cinematic join — transitions, sound, grade — happens in
**Premiere Pro**, not FFmpeg and not in LOTUS. Don't add an FFmpeg concat
step to this path.
