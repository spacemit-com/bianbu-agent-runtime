---
schema_version: 2
name: drawio
name_zh: Draw.io 图表
name_en: Draw.io Diagrams
description: "Draw.io (diagrams.net) diagram creation, editing, and review. Use for: (1) Creating architecture/system/flow diagrams in .drawio XML format, (2) Editing existing .drawio files directly (XML), (3) Converting .drawio to PNG/SVG/PDF, (4) Layout adjustment of diagram elements, (5) AWS icon usage in diagrams."
description_zh: "Draw.io（diagrams.net）图表的创建、编辑与检查。适用于：(1) 用 .drawio XML 格式绘制架构/系统/流程图；(2) 直接编辑已有 .drawio 文件；(3) 把 .drawio 导出为 PNG/SVG/PDF；(4) 调整图元布局；(5) 在图中使用 AWS 图标。"
---

# Draw.io Diagram Skill

## File Types

- Edit only `.drawio` files (plain XML format)
- `.drawio.png` files are auto-generated exports — do NOT edit directly
- Export with transparent backgrounds for versatile use

## Conversion Command

Use the draw.io CLI to export to PNG:

```sh
drawio -x -f png -s 2 -t -o output.drawio.png input.drawio
```

| Option | Description |
|--------|-------------|
| `-x` | Export mode |
| `-f png` | PNG format |
| `-s 2` | 2x scale (high resolution) |
| `-t` | Transparent background |
| `-o` | Output path |

## Editing .drawio XML

Open `.drawio` file in text editor. Key XML structure:

- **`mxCell`** — Each diagram element
- **`mxGeometry`** — Position and size: `x`, `y`, `width`, `height`
- Element center coordinate = `y + (height / 2)`

### Font Settings
For documents (e.g., Quarto slides), specify `defaultFontFamily` in mxGraphModel tag and `fontFamily` in each text element's style:

```xml
<mxGraphModel defaultFontFamily="Noto Sans JP" ...>
```
```xml
style="text;html=1;fontSize=27;fontFamily=Noto Sans JP;"
```

## Design Principles

### Basic
- Clarity: Simple, visually clean diagrams
- Consistency: Unify colors, fonts, icon sizes, line thickness
- Accuracy: Do not sacrifice accuracy for simplification

### Element Rules
- Label all elements
- Use arrows to indicate direction (prefer 2 unidirectional over bidirectional)
- Use latest official icons
- Add legend to explain custom symbols
- Remove decorative icons irrelevant to context

### Accessibility
- Ensure sufficient color contrast
- Use patterns in addition to colors

### Progressive Disclosure
Split complex systems into staged diagrams:

| Type | Purpose |
|------|---------|
| Context Diagram | System overview from external perspective |
| System Diagram | Main components and relationships |
| Component Diagram | Technical details and integration points |
| Deployment Diagram | Infrastructure configuration |
| Data Flow Diagram | Data flow and transformation |
| Sequence Diagram | Time-series interactions |

### Metadata
Include title, description, last updated, author, and version.

## Layout Best Practices

### Arrow Placement
- Place arrows in XML right after Title (back layer)
- Avoid arrows overlapping labels
- Keep arrow start/end at least 20px from label edges
- For text elements, use explicit coordinates (sourcePoint/targetPoint)

### Background Frame Margins
Internal elements must have **at least 30px margin** from frame boundary:

```
Background frame: y=20, height=400 → range y=20–420
Internal top: frame y + 30 or more (e.g., y=50)
Internal bottom: frame y + height − 30 or less (e.g., up to y=390)
```

### Text Width
- Allow 30–40px per character
- Use `<br>` for line breaks in labels
- Insufficient width causes unintended line breaks

### Edge Labels
Adjust offset attribute to distance labels from arrows:
```xml
<mxPoint x="0" y="-40" as="offset"/>  <!-- Above arrow -->
<mxPoint x="0" y="40" as="offset"/>   <!-- Below arrow -->
```

## Output Checklist

- [ ] No background color set (transparent)
- [ ] Font size appropriate (larger recommended)
- [ ] Arrows placed at back layer
- [ ] Arrows not overlapping labels
- [ ] Internal elements not overflowing background frame
- [ ] 30px+ margin between frame and internal elements
- [ ] AWS service names are correct
- [ ] No unnecessary elements remaining
- [ ] Visually verify PNG output

## Fallback Without CLI

If `drawio` CLI is not installed, generate a diagrams.net online editing link: `https://app.diagrams.net/` and instruct the user to paste the .drawio XML content there.
