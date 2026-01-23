# Minus - Visual Design Guide

## Design Philosophy

**Theme:** Hacker/Terminal aesthetic inspired by Mr. Robot
**Mood:** Dark, technical, purposeful, slightly retro-futuristic

## Color Palette

| Color | Hex | Usage |
|-------|-----|-------|
| **Background** | `#000000` | Primary background (pure black) |
| **Card Background** | `#0d0d0d` | Section/card backgrounds |
| **Secondary Background** | `#0a0a0a` | Nested elements, inputs |
| **Matrix Green** | `#00ff41` | Primary accent, success states, active elements |
| **Danger Red** | `#ff0040` | Blocking state, errors, destructive actions |
| **Purple** | `#a855f7` | VLM indicators, vocabulary highlights |
| **Warning Yellow** | `#ffcc00` | Paused state, warnings |
| **Text Primary** | `#e0e0e0` | Main text |
| **Text Secondary** | `#707070` | Labels, secondary info |
| **Text Muted** | `#505050` | Disabled, timestamps |
| **Border** | `#1a1a1a` | Default borders |
| **Border Highlight** | `#333333` | Hover/focus borders |

## Typography

### Fonts

| Font | Usage |
|------|-------|
| **VT323** | Logo, section titles, buttons, badges - Terminal/retro display font |
| **IBM Plex Mono** | Body text, data values, logs - Readable monospace |
| **DejaVu Sans Bold** | TV overlay vocabulary text - Clear, readable at distance |
| **DejaVu Sans Mono** | TV overlay stats - Monospace for alignment |

### Text Hierarchy

- **Logo "MINUS"**: VT323, 2.5-3rem, white-to-red gradient, blinking cursor
- **Section Headers**: VT323, 1.5rem, uppercase, white, letter-spacing 0.1em
- **Tab Navigation**: VT323, 1.1rem, uppercase
- **Body Text**: IBM Plex Mono, 14px, light gray
- **Labels**: IBM Plex Mono, 0.8rem, uppercase, muted
- **Data Values**: VT323, 1.3rem, accent colors with glow

## Visual Effects

### Glows & Shadows
- Active elements: `box-shadow: 0 0 15px rgba(color, 0.3)`
- Text glow: `text-shadow: 0 0 8px rgba(color, 0.4)`
- Blocking state: Red glow on borders and background

### Animations
- **Cursor blink**: 1s step-end infinite (logo cursor)
- **Pulse**: 1s infinite opacity (status indicators)
- **Glow pulse**: 2s infinite (connected status)
- **Blocking pulse**: 1s infinite background color shift

### Background
- Pure black with subtle matrix grid pattern
- Grid: 20px spacing, `rgba(0, 255, 65, 0.03)` lines
- Scanline overlay: 30% opacity horizontal lines

## Component Styling

### Buttons
- Transparent background with colored border
- Fill on hover with glow effect
- All uppercase VT323 text
- Keyboard shortcut hints in `<kbd>` tags

### Cards/Sections
- Dark card background (#0d0d0d)
- 1px border, subtle hover glow
- Section headers with bottom border

### Status Indicators
- Colored left border (3px) for state indication
- Background tint matching state color
- Badges: bordered, colored text, slight background tint

### Toggle Switches
- Dark track, moves to green glow when active
- Square/rectangular aesthetic (not rounded)

## TV Overlay Design

### YUV Color Values for ustreamer API

The TV overlay uses YUV color space via the ustreamer blocking API:

| Color | Hex | Y | U | V | Usage |
|-------|-----|---|---|---|-------|
| **White** | `#ffffff` | 235 | 128 | 128 | Header, translation text |
| **Purple** | `#a855f7` | 128 | 195 | 156 | Spanish word (prominent) |
| **Gray** | `#707070` | 112 | 128 | 128 | Pronunciation, example |
| **Matrix Green** | `#00ff41` | 151 | 82 | 30 | Available for stats |
| **Danger Red** | `#ff0040` | 84 | 117 | 250 | Available for alerts |
| **Black** | `#000000` | 16 | 128 | 128 | Background box |

**Multi-color API Parameters:**
```
text_y, text_u, text_v         # Default text color (white - header/translation)
word_y, word_u, word_v         # Spanish word color (purple)
secondary_y, secondary_u, secondary_v  # Pronunciation/example (gray)
```

### TV Overlay Fonts

Fonts are compiled into ustreamer - using clean, readable fonts for TV viewing:

| Element | Font | Path |
|---------|------|------|
| **Spanish Word** | IBM Plex Mono Bold | `/usr/share/fonts/truetype/ibm-plex/IBMPlexMono-Bold.ttf` |
| **Header/Translation/etc** | DejaVu Sans Bold | `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf` |
| **Stats** | IBM Plex Mono Regular | `/usr/share/fonts/truetype/ibm-plex/IBMPlexMono-Regular.ttf` |
| **Fallback (word)** | IBM Plex Mono SemiBold | `/usr/share/fonts/truetype/ibm-plex/IBMPlexMono-SemiBold.ttf` |
| **Fallback (vocab)** | FreeSans Bold | `/usr/share/fonts/truetype/freefont/FreeSansBold.ttf` |
| **Fallback (stats)** | DejaVu Sans Mono | `/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf` |
| **Bitmap fallback** | font8x8 | Built-in 8x8 pixel font |

To change fonts, modify `FONT_PATH_*` defines in `/home/radxa/ustreamer-garagehq/src/libs/blocking.c` and recompile ustreamer with `make WITH_MPP=1`.

### Vocabulary Text Layout

The TV overlay vocabulary display matches the web UI "Current Word" card with per-line colors:

```
[ BLOCKING // SOURCE ]     <- White (header)

spanish_word               <- Purple (word_color)
(pronunciation)            <- Gray (secondary_color)

= english translation      <- White (text_color)

"Example sentence"         <- Gray (secondary_color)
```

**Color Detection:**
The ustreamer blocking code automatically detects line type by first character:
- Lines starting with `[` → white (header)
- Lines starting with `(` → gray (pronunciation)
- Lines starting with `=` → white (translation)
- Lines starting with `"` → gray (example)
- Other non-empty lines → purple (Spanish word)

### Blocking Overlay
The blocking overlay appears on the TV when ads are detected.

**Layout:**
```
+--------------------------------------------------+
|                                                  |
|              BLOCKING (SOURCE)                   |  <- Header (top center)
|                                                  |
|                  palabra                         |  <- Spanish word (large)
|              (pronunciation)                     |  <- Pronunciation
|              = translation                       |  <- English translation
|                                                  |
|         Example sentence in Spanish.             |  <- Example sentence
|                                                  |
|  +--------+                         +----------+ |
|  | STATS  |                         | PREVIEW  | |  <- Stats (BL), Preview (BR)
|  +--------+                         +----------+ |
+--------------------------------------------------+
```

**Header "BLOCKING (SOURCE)":**
- Font: DejaVu Sans Bold (or bitmap font for retro look)
- Color: White text
- Position: Top center
- Style: All caps, letter-spacing

**Vocabulary Text:**
- Spanish word: Large, purple/accent color, bold
- Pronunciation: Smaller, italic, muted
- Translation: White, preceded by "="
- Example: Italic, muted color
- Font: DejaVu Sans Bold for readability at TV distance

**Stats Dashboard (bottom-left):**
- Font: DejaVu Sans Mono (monospace for alignment)
- Color: Green (#00ff41) for values
- Content: Uptime, ads blocked, block time

**Preview Window (bottom-right):**
- Live preview of blocked content
- Border: Subtle, matches theme

**Background:**
- Pixelated/blurred capture of pre-ad screen
- Darkened (60% brightness)
- 20x downscale for pixelation effect

### No Signal Overlay
- Text: "NO SIGNAL" in large VT323-style font
- Color: Red (#ff0040)
- Animation: Subtle flicker
- Background: Pure black

### Loading Overlay
- Text: "LOADING..." or "INITIALIZING"
- Color: Green (#00ff41)
- Animation: Pulse or typing effect
- Optional: Progress indicator

## State Colors Summary

| State | Primary Color | Background Tint |
|-------|--------------|-----------------|
| Normal/Monitoring | Green `#00ff41` | None |
| Blocking | Red `#ff0040` | `rgba(255, 0, 64, 0.1)` |
| Paused | Yellow `#ffcc00` | `rgba(255, 204, 0, 0.05)` |
| VLM Active | Purple `#a855f7` | `rgba(168, 85, 247, 0.1)` |
| Error | Red `#ff0040` | None |
| Loading | Green (pulsing) | None |

## Responsive Considerations

- Mobile: Stack elements vertically, larger touch targets
- Desktop: Side-by-side layouts where appropriate
- TV Overlay: Large text readable from couch distance (~10ft)

## Accessibility Notes

- High contrast between text and backgrounds
- Status not conveyed by color alone (also text/icons)
- Animations can be reduced if needed
- Minimum text size: 14px for body, larger for TV overlays
