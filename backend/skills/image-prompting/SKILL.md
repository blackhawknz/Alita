---
name: image-prompting
description: Craft high-quality prompts and settings for local image generation with easy_diffusion. Use whenever the user asks for an image, illustration, render or artwork.
tags: [images, creative]
---

# Image prompting (EasyDiffusion)

## Steps
1. Pin down subject, style, mood, composition and aspect ratio. Ask only if the request is very vague.
2. Write the prompt from most to least important:
   `subject + details, setting, style/medium, lighting, camera/composition, quality tags`.
   Example: `a red fox curled up in fresh snow, birch forest at dawn, soft golden rim light, shallow depth of field,
   35mm photo, highly detailed`.
3. Add a negative prompt such as `blurry, low quality, deformed, extra limbs, watermark, text, jpeg artifacts`.
4. Choose a size that matches the aspect ratio: 512x768 for portrait, 768x512 for landscape, 512x512 or larger for square.
   Use 25-30 steps and guidance 7-7.5 unless the user asks otherwise.
5. Show each result inline in the reply with `![short description](<url from the tool result>)`, and give the seed so it can be reproduced.

## Iterating
- To keep the composition while changing details, reuse the seed and edit the prompt.
- For variety, generate 2-4 images with different seeds.
- If EasyDiffusion is unreachable, tell the user to start it (default http://localhost:9000) or change the URL in Settings.
