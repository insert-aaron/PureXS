# PureChart API Reference

Chat context captured 2026-04-24 — Q: "how does PureChart patient fetch api look like?"

Source of truth: [purechart.py](purechart.py) (Python) and [PureXS.WPF/Services/PureChartService.cs](PureXS.WPF/Services/PureChartService.cs) (C# mirror).

---

## Patient Search (Phase 1-2)

### Endpoint
```
POST https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/xray-patient-search
```
Defined at [purechart.py:24-26](purechart.py#L24-L26).

### Headers
Set once on the `requests.Session` in [purechart.py:68-73](purechart.py#L68-L73):

| Header          | Value                                   |
| --------------- | --------------------------------------- |
| `Authorization` | `Bearer <SUPABASE_ANON_KEY>`            |
| `apikey`        | `<SUPABASE_ANON_KEY>`                   |
| `x-api-key`     | `<facility_token>` (per-clinic secret)  |
| `Content-Type`  | `application/json`                      |

The anon key is baked into the client at [purechart.py:30-35](purechart.py#L30-L35). The facility token is supplied at loader construction time.

### Request Body
```json
{ "q": "<search query>" }
```
Sent from [purechart.py:80-84](purechart.py#L80-L84). Timeout: 15 s.

### Response
The edge function may return either:
- a bare JSON array of patient records, **or**
- `{ "patients": [ ... ] }`

Up to 15 results. Parsed in [purechart.py:88-102](purechart.py#L88-L102).

### Patient Model
`PureChartPatient` — [purechart.py:38-55](purechart.py#L38-L55):

| Field                   | Type  |
| ----------------------- | ----- |
| `id`                    | str   |
| `first_name`            | str   |
| `last_name`             | str   |
| `medical_record_number` | str   |
| `dob`                   | str   |
| `phone`                 | str   |
| `profile_picture_url`   | str   |

`display_name` → `"{first} {last} — {MRN}"`.

### Threading
`requests` is blocking — GUI must call `PureChartPatientLoader.search()` from a background thread.

---

## X-Ray Upload (Phase 3)

### Endpoint
```
POST https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/upload-xray
```
Defined at [purechart.py:27-29](purechart.py#L27-L29).

### Headers
Same as search — Bearer + apikey + x-api-key + JSON. See [purechart.py:152-158](purechart.py#L152-L158).

### Request Body
Sent from [purechart.py:178-185](purechart.py#L178-L185):

```json
{
  "patientId": "<uuid>",
  "base64Data": "<base64-encoded file bytes>",
  "contentType": "image/png | image/jpeg | image/tiff | image/bmp | application/pdf | application/dicom",
  "type": "xrays | panoramic_xray | bitewings | periapical",
  "title": "<display title>",
  "originalFilename": "<basename.ext>"
}
```

### Exam-Type → Upload-Type Map
[purechart.py:108-116](purechart.py#L108-L116):

| Exam                | `type`           |
| ------------------- | ---------------- |
| Panoramic           | `panoramic_xray` |
| Ceph Lateral        | `xrays`          |
| Ceph Frontal        | `xrays`          |
| Bitewing Left       | `bitewings`      |
| Bitewing Right      | `bitewings`      |
| Bitewing Bilateral  | `bitewings`      |
| Periapical          | `periapical`     |

### Content-Type Map
[purechart.py:118-127](purechart.py#L118-L127): `.png → image/png`, `.jpg/.jpeg → image/jpeg`, `.tiff/.tif → image/tiff`, `.bmp → image/bmp`, `.pdf → application/pdf`, `.dcm → application/dicom`. Default falls back to `image/png`.

### Response → `UploadResult`
Parsed in [purechart.py:190-200](purechart.py#L190-L200):

| Field            | JSON key          |
| ---------------- | ----------------- |
| `success`        | `success`         |
| `file_url`       | `fileUrl`         |
| `attachment_id`  | `attachmentId`    |
| `patient_id`     | `patientId`       |
| `filename`       | `filename`        |
| `upload_type`    | `type`            |
| `size`           | `size`            |
| `error`          | `error` or `message` |
| `http_status`    | (from HTTP)       |

Non-2xx responses set `success=False` and populate `error` with `HTTP <code>` when no message is returned — [purechart.py:202-206](purechart.py#L202-L206). Timeout: 60 s.
