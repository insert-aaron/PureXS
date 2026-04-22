# PureXS Color Theme Specification

## Brand Colors (Fixed across both themes)

| Token | Hex | Purpose |
|-------|-----|---------|
| Dental Blue | `#2563EB` | Primary brand |
| Dental Green | `#10B981` | Success/captured |
| Dental Accent | `#3B82F6` | Active/accent |

## System Accent Scale

`#1E3A8A` → `#1E40AF` → `#1D4ED8` → `#2563EB` → `#3B82F6` → `#60A5FA` → `#93C5FD`

## Dark Mode

| Element | Color |
|---------|-------|
| Top Bar Gradient | `#0F172A` → `#1E3A5F` → `#1D4ED8` |
| Patient Bar (Success) | `#0A1F15` |
| Banner (Success) | `#064E3B` |
| Patient Bar Text | `#D1FAE5` |
| Banner Text Secondary | `#6EE7B7` |
| Slot Code Overlay | `rgba(10, 15, 26, 0.8)` |
| Eye Button BG | `rgba(17, 24, 39, 0.8)` |
| Eye Button FG | `#E2E8F0` |
| Captured Slot BG | `#0F2A1C` |
| Active Slot BG | `#0D1829` |
| FMX Pending Border | `#374151` |
| Acrylic Element (Win10 fallback) | `#1E1E1E` @ 92% |
| Acrylic Window (Win10 fallback) | `#181818` @ 88% |

## Light Mode (PureChart-inspired)

| Element | Color |
|---------|-------|
| Top Bar Gradient | `#1D4ED8` → `#2563EB` → `#3B82F6` |
| Patient Bar (Success) | `#ECFDF5` |
| Banner (Success) | `#D1FAE5` |
| Patient Bar Text | `#065F46` |
| Banner Text Secondary | `#047857` |
| Slot Code Overlay | `rgba(248, 250, 255, 0.87)` |
| Eye Button BG | `rgba(239, 246, 255, 0.87)` |
| Eye Button FG | `#1E3A5F` |
| Captured Slot BG | `#F0FDF4` |
| Active Slot BG | `#EFF6FF` |
| FMX Pending Border | `#CBD5E1` |

## Slot Border States (Fixed)

| State | Color |
|-------|-------|
| Pending | `#374151` |
| Captured | `#10B981` |
| Active | `#3B82F6` |
| Danger | `#E81123` |

## AI Detection Overlay Colors

| Detection | Color |
|-----------|-------|
| Caries | `#EF4444` (red) |
| Deep Caries | `#F97316` (orange) |
| Impacted | `#EAB308` (yellow) |
| Periapical Lesion | `#A855F7` (purple) |

## Additional UI Details

- **Framework:** WPF (.NET 8) with custom theme engine (`App.ApplyTheme()`)
- **Backdrop:** `WindowBg` brush; borderless window with custom chrome
- **Typography:** Segoe UI + Consolas for monospace; PatientTitleTextBlockStyle at 16px SemiBold
- **Elevation:** Card shadows at 18% black / 24px blur; Overlay shadows at 35% black / 48px blur
- **Corner Radii:** Panels 12px, Buttons 8px, Expose 12px, Avatars 18px (Apple squircle)
- **Theme switching:** `App.ToggleTheme()` / `App.ApplyTheme(isDark)` swaps 40+ `DynamicResource` brushes at runtime
- **Theme definitions:** `App.xaml` holds default (dark) brush resources; `App.xaml.cs` contains dark/light value maps
