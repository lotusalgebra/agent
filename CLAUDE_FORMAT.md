# Claude.ai house format for LOTUS

Paste the block below as the FIRST message of any content-generation chat
in Claude.ai. The parser accepts files in this format with 100% accuracy
(every prompt classified as `code_block` source — no Gemma drift, no
missing items, no need to re-edit afterwards).

---

## The prompt to paste

```
HOUSE FORMAT — FOLLOW EXACTLY (Lotus parser eats this with 100% accuracy)

Use this structure for every slide, story, cover, frame, or anything you
want rendered as an image. Heading levels MUST match exactly.

## SLIDE 1 — <short title>
### NB2 PROMPT
` ` `
<prompt body — full scene description, baked text, palette, multi-line>
` ` `

## SLIDE 2 — <title>
### NB2 PROMPT
` ` `
<prompt body>
` ` `

## STORY 1 — 8:00 AM <short title>
### NB2 PROMPT
` ` `
<prompt body>
` ` `

## STORY 2 — 8:15 AM <short title>
### NB2 PROMPT
` ` `
<prompt body>
` ` `

## REEL COVER
### NB2 PROMPT
` ` `
<prompt body>
` ` `

RULES
1. Heading order is fixed: `##` (item) then `### NB2 PROMPT` then a
   fenced code block.
2. NEVER put a renderable prompt as bold paragraphs, bullets, tables,
   or quotes — only inside a fenced code block under `### NB2 PROMPT`.
3. The phrase "NB2 PROMPT" is required on every code block — if missing,
   the parser may drop it.
4. One prompt per code block. Don't mix caption + prompt + hashtags in
   the same block.
5. Non-image content (POSTING SCHEDULE, PIN COMMENT, PRE-POST CHECKLIST,
   COLOR PALETTE, CAPTION) goes under headings that do NOT contain
   "PROMPT". Example: `## 💬 PIN COMMENT` — these are skipped
   automatically by Lotus.
6. If you have N slides + M stories, output exactly N+M code blocks, each
   under its own `##` heading.

Output the entire MD file in a single message.
```

(Replace each ` ` ` line above with three consecutive backticks — they're
spaced out here only because this saved file itself uses fenced code.)

---

## How the parser sees it

| Format you give Claude | Parser source | Accepted? |
|---|---|---|
| `## SLIDE/STORY N` + `### NB2 PROMPT` + fenced block | `code_block` | **Yes — auto, no Gemma** |
| `## SLIDE/STORY N` + body without code block | `section` | Maybe — depends on Gemma classifier |
| Bold paragraphs / bullets (the old story style) | `story_section` (A19) | Now yes, but fragile |
| Plain prose anywhere | `section` | Likely no |

When in doubt: every prompt you want rendered MUST be inside a fenced
code block, and that block's heading hierarchy MUST contain "PROMPT"
somewhere.

---

## Quick conversion request for an existing messy file

If a file is already in a different format and you want Claude to convert
it to the house format, paste the block above THEN add:

> Now convert the following file into the HOUSE FORMAT. Preserve every
> visual prompt; for non-prompt sections (schedule, checklists, captions),
> keep them under headings that don't contain "PROMPT".

Followed by the full file content.
