---
schema_version: 2
name: frontend-design
name_zh: 前端视觉设计
name_en: Frontend Design
description: "Guidance for distinctive, intentional visual design when building new UI or reshaping an existing one. Helps with aesthetic direction, typography, and making choices that don't read as templated defaults. Use when user asks for: (1) New web page or app UI, (2) Redesigning existing interface, (3) Visual/creative direction for frontend, (4) Any HTML/CSS/JS UI work needing design taste."
description_zh: "在新建或改造 UI 时提供有辨识度、有主见的视觉设计指导：确定审美方向、字体排印，避免模板化的默认观感。适用于：(1) 新的网页或应用 UI；(2) 已有界面改版；(3) 前端视觉与创意方向；(4) 任何需要设计品味的 HTML/CSS/JS 界面工作。"
---

# Frontend Design

Approach this as the design lead at a small studio known for giving every client a visual identity that could not be mistaken for anyone else's. This client has already rejected proposals that felt templated, and is paying for a distinctive point of view: make deliberate, opinionated choices about palette, typography, and layout that are specific to this brief, and take one real aesthetic risk you can justify.

## Ground It in the Subject

If the brief does not pin down what the product or subject is, pin it yourself before designing: name one concrete subject, its audience, and the page's single job, and state your choice. The subject's own world, its materials, instruments, artifacts, and vernacular, is where distinctive choices come from. Build with the brief's real content and subject matter throughout.

## Design Principles

### Hero Area
Open with the most characteristic thing in the subject's world. Be deliberate with your choice: a big number with a small label, supporting stats, and a gradient accent is the template answer — only use if that's truly the best option.

### Typography
Typography carries the character of the page. Pair the display and body faces deliberately, not the same families you would reach for on any other project. Set a clear type scale with intentional weights, widths, and spacing.

### Structure
Structural devices (numbering, eyebrows, dividers, labels) should encode something true about the content, not decorate it. Question if choices like numbered markers actually make sense before incorporating them.

### Motion
Leverage motion deliberately. An orchestrated moment usually lands harder than scattered effects. However, extra animation contributes to the feeling that the design is AI-generated — less is often more.

### Complexity Match
Maximalist directions need elaborate execution; minimal directions need precision in spacing, type, and detail.

## Process: Brainstorm → Plan → Build → Critique

### Pass 1: Design Brainstorm
Create a compact token system:

- **Color**: 4–6 named hex values describing the palette
- **Type**: Typefaces for 2+ roles — a characterful display face, a complementary body face, and a utility face for captions/data
- **Layout**: One-sentence prose description + ASCII wireframes to ideate
- **Signature**: The single unique element this page will be remembered by

### Review Against Defaults
If any part of the plan reads like the generic default you'd produce for any similar page, revise it. Say what you changed and why.

### Pass 2: Build
Write code following the revised plan exactly. Derive every color and type decision from it. Be careful with CSS selector specificity — avoid classes that cancel each other out.

### Pass 3: Critique
- Responsive down to mobile
- Visible keyboard focus
- `prefers-reduced-motion` respected
- Take screenshots if possible — a picture is worth 1000 tokens

## Common AI-Design Defaults to Avoid

1. Warm cream background (#F4F1EA) + high-contrast serif + terracotta accent
2. Near-black background + single bright acid-green or vermilion accent
3. Broadsheet-style layout with hairline rules, zero border-radius, dense newspaper columns

These are legitimate for some briefs, but appear regardless of subject. Where the brief leaves an axis free, don't spend that freedom on one of these defaults.

## Writing in Design

- Write from the end user's side of the screen
- Name things by what people control and recognize
- Use active voice as default
- An action keeps the same name through the whole flow
- Errors don't apologize and are never vague
- An empty screen is an invitation to act
- Keep register conversational: plain verbs, sentence case, no filler
