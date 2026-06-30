# Session — Multi-Facility Toggle + "Auto from Server" Names (2026-06-30)

## Goal

Let an operator toggle between multiple PureChart facilities, each with its own
`x-api-key`, so switching renders that facility's correlated patients. Facilities
are added/managed through a settings UI. Built in **both** apps per the
dual-implementation rule (`purexs_gui.py` and `PureXS.WPF`).

## Design decisions (confirmed with user)

| Question | Choice |
|---|---|
| Where the toggle lives | **Dropdown in the top toolbar** (reloads the patient dock on switch) |
| How facilities are identified | **Auto from server** (real facility name), with editable fallback |
| Settings entry point | **⚙ gear button** in the toolbar → facilities-management dialog |

## Config schema (`config.json`, both apps — back-compat preserved)

```json
{
  "facilities": [{ "name": "Austin", "token": "<64-char x-api-key>" }],
  "active_facility": 0,
  "facility_token": "<mirror of active token — kept for legacy readers>"
}
```

- A legacy single `facility_token` **auto-migrates** into a one-entry list on load.
- The active facility's token is always mirrored back to `facility_token`, so the
  401 re-prompt path and any other legacy reader keep working.
- Python config dir: `~/.purexs/`. WPF config dir: `%AppData%/PureXS/`.

## The "auto from server" gap — and how it was closed

**Initial finding:** the deployed `xray-gateway` backend mapped a token → facility
*server-side only* and **never returned a facility name** — `/search` and
`/scheduled` responses were just `{patients:[...]}`. There was no name field for
the client to read.

**Client-side (shipped first, future-proof):** both apps now read
`facility_name` / `facilityName` / `facility(.name)` from the `/scheduled`
response **if present**, falling back to a `Facility <last-4-of-token>`
placeholder otherwise. Name resolution runs on a **background thread** (never
blocks startup) and only overwrites names still matching the `"Facility "`
placeholder — never a user-typed name.

**Backend change (made this session, in the `purechart-app` repo):** added the
real `facility_name` to the gateway responses so auto-naming actually works.

## Backend edit — `purechart-app/supabase/functions/xray-gateway/index.ts`

The dispatcher already resolved `facilityId` from the token (via
`facility_external_tokens`). Added:

1. **`resolveFacilityName(supabase, facilityId)`** — selects `facilities.name`
   (`.is("deleted_at", null).maybeSingle()`). Confirmed `facilities.name` is the
   real column (matches `profile-update-validate-token`'s `select("id, name, logo_url")`).
2. **`withFacilityName(resp, name)`** — splices `facility_name` into the JSON
   object body, re-emitting through the existing `json()` helper (preserves
   status/headers). No-op for non-JSON/array bodies.
3. **Dispatcher** — resolves the name **once**, wraps the `search` and
   `scheduled` route responses (incl. empty `{patients:[]}`, so a name resolves
   even on zero-appointment days). One PK-indexed lookup per call — negligible.

`upload` / `photo` routes untouched.

### Deploy + verification (live, production `whzohbzqhqaohpohmqah`)

```bash
supabase functions deploy xray-gateway --project-ref whzohbzqhqaohpohmqah
```

> Note: the user's *first* deploy was a no-op for this feature — it re-uploaded
> the unedited file. The edit had to land **before** deploying. After the real
> edit + redeploy, verified live:

```
/scheduled → keys: ['patients', 'facility_name'],  facility_name => 'Austin',  17 patients
/search    → keys: ['patients', 'facility_name'],  facility_name => 'Austin',  15 patients
```

✅ Auto-from-server names are now genuinely live. No further client changes needed.

## Files changed

### `purechart-app` (backend)
- `supabase/functions/xray-gateway/index.ts` — `resolveFacilityName`,
  `withFacilityName`, dispatcher wrap.

### PureXS — Python
- `purechart.py` — `fetch_facility_name(token)` (reads `facility_name` etc.).
- `purexs_gui.py`:
  - Config layer: `_read_config` / `_write_config` / `_write_facilities` /
    `_load_facilities` (+ legacy migration) / `_load_active_facility_index` /
    `_active_token` / `_active_facility_name` / `_derive_facility_name` (no-net) /
    `_rebuild_purechart_clients`. `_save_facility_token` now updates the **active**
    facility's token; `_prompt_facility_token` no longer auto-saves.
  - Toolbar: `CTkOptionMenu` facility switcher + `⚙` button.
  - `_on_facility_selected` / `_switch_facility` / `_refresh_facility_menu` /
    `_unique_facility_labels` / `_open_facility_settings`.
  - Background names: `_resolve_facility_names_bg` / `_resolve_names_worker` /
    `_on_names_resolved` (scheduled ~900 ms after launch).
  - New `FacilitySettingsDialog(ctk.CTkToplevel)` — rows tracked by stable `rid`,
    active via radio; add/edit/remove; Save persists + reloads dock.

### PureXS — WPF (`PureXS.WPF`)
- `Models/FacilityConfig.cs` — new `ObservableObject` (Name/Token/IsActive +
  `DerivePlaceholderName`).
- `Services/IConfigService.cs` + `ConfigService.cs` — `Facilities`,
  `ActiveFacilityIndex`, `SaveFacilities`, `SetActiveFacility`, `LoadFacilities`
  migration. `SaveFacilityToken` updates the active facility.
- `Services/IPureChartService.cs` + `PureChartService.cs` —
  `FetchFacilityNameAsync` (one-off `HttpClient` with the probed token).
- `ViewModels/MainViewModel.cs` — `Facilities` `ObservableCollection` +
  `SelectedFacility` (`OnSelectedFacilityChanged` switches; `_suppressFacilitySwitch`
  guards repopulation), `OpenFacilitySettingsCommand`, `ReloadFacilities`,
  `ResolveFacilityNamesAsync`. 401 path refreshes the toggle.
- `Views/FacilitySettingsDialog.xaml` + `.xaml.cs` — ItemsControl rows,
  single-active `RadioButton` (`GroupName` + TwoWay `IsActive`).
- `Views/MainWindow.xaml` — top-bar `ComboBox` (repurposed spare cols 4/5) + gear
  `Button` (`&#xE713;`) → settings.
- `App.xaml.cs` — seeds + persists a facility on first launch so the toggle has an
  entry; env `PURECHART_FACILITY_TOKEN` still overrides the initial service token.

## Verification status

- **Python** — imports clean; config migration/switch/401-reprompt/first-launch
  logic exercised across 4 paths (all green).
- **WPF** — builds **0 warnings / 0 errors** (`-p:EnableWindowsTargeting=true`).
- **Backend** — `facility_name` confirmed live on `/scheduled` and `/search`.
- **Not yet** — run on real hardware / full app launch confirming the dropdown
  renders `Austin` end-to-end.

## Behavior notes

- Switching the dropdown: persists active index, swaps the service token, resets
  the once-per-session 401 reprompt, and reloads the dock for the new facility.
- Background resolver overwrites only placeholder names. To force a re-pull of a
  manually edited name, clear the name field in ⚙ settings and Save (blank →
  re-derives from server).
- `/scheduled` returns `facility_name` even with zero scheduled patients, so name
  resolution works on any day.
