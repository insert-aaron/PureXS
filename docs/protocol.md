# SiNet2 / P2K Protocol Reference

> **Status:** Reverse-engineered from `SiNet2.dll` and `SiPanCtl.dll`.
> All offsets and field semantics have been verified against live hardware captures.

---

## 1. Overview

**SiNet2** (also called **P2K — Protocol 2000**) is the proprietary binary
network protocol used by Sirona dental imaging devices to communicate with
the Sidexis host software.  It operates over both **UDP** (device discovery)
and **TCP** (command/data sessions) on **port 1999** by default.

Known hardware targets:

| Device type code | Product name     | Modality         |
|-----------------|------------------|------------------|
| `0x0001`        | ORTHOPHOS XG     | Panoramic / Cephalo |
| `0x0002`        | ORTHOPHOS SL     | Panoramic / Cephalo |
| `0x0003`        | GALILEOS         | CBCT 3D          |
| `0x0004`        | HELIODENT DS     | Intraoral sensor |
| `0x0005`        | ORTHOPHOS XG 3D  | Panoramic + CBCT |
| `0x0006`        | ORTHOPHOS SL 3D  | Panoramic + CBCT |
| `0x0010`        | DIGORA           | Phosphor plate   |
| `0x0020`        | SIDEXIS SERVER   | Host software    |

---

## 2. Magic Word

Every SiNet2 frame, regardless of transport, begins with the 16-bit magic
word **`0x072D`**.

- On UDP: stored at offset `+0x00` as a little-endian WORD.
- On TCP: stored at offset `+0x00` as the low 16 bits of a big-endian DWORD
  (`0x0000072D`).

---

## 3. UDP Transport — Device Discovery

### 3.1 Port Assignment

| Direction       | Port  | Notes                                    |
|-----------------|-------|------------------------------------------|
| Probe (→ device)| 1999  | Device listens on this port              |
| Reply (→ host)  | any   | Device replies to the sender's address   |
| Host listen     | 55999 | Recommended high port (no root required) |

The host sends a **broadcast** probe to `255.255.255.255:1999` and listens
for unicast replies.

### 3.2 UDP Frame Header (18 bytes, little-endian)

```
Offset  Size  Field         Value / Notes
──────  ────  ────────────  ─────────────────────────────────────────────
+0x00   WORD  magic         0x072D
+0x02   WORD  reserved      0x0000
+0x04   WORD  func_code     0x8000 = FUNC_DISCOVER
+0x06   WORD  reserved      0x0000
+0x08   WORD  reserved      0x0000
+0x0A   WORD  api_version   e.g. 0x0001
+0x0C   WORD  payload_len   14 + len(payload)  (0x000E for empty payload)
+0x0E   WORD  reserved      0x0000
+0x10   WORD  seq_num       monotonically increasing per sender
```

Total header size: **18 bytes**.
`payload_len` covers bytes from offset `+0x04` through end-of-frame, so an
empty payload gives `payload_len = 0x000E` (14 bytes from `func_code` to
`seq_num` inclusive).

### 3.3 Discovery Probe

The probe is a header-only frame with no payload (`payload_len = 0x000E`):

```
2D 07 00 00 00 80 00 00 00 00 01 00 0E 00 00 00 00 00
```

### 3.4 Discovery Response Payload

The device echoes the header (with its own `api_version` and `seq_num`) and
appends a fixed-order payload of **7 fields** encoded in **big-endian**:

```
Field #  Name                Type  Description
───────  ──────────────────  ────  ──────────────────────────────────────────
1        NameP2K             S     TCP port number as decimal ASCII in UTF-16LE
2        DefGatewayAddress   BA    4 bytes: default gateway IPv4 address
3        SubNetMask          BA    4 bytes: subnet mask
4        IpAddress           BA    4 bytes: device IPv4 address
5        EthernetAddress     BA    6 bytes: device MAC address
6        ConfigTime          W     Configuration timestamp (opaque WORD)
7        DeviceType          W     Device type code (see table in §1)
```

---

## 4. Payload Field Encoding

All payload fields use big-endian byte order regardless of transport layer.

### 4.1 Type S — UTF-16LE String

```
+0  4 bytes  BE DWORD  char_count   Number of UTF-16LE code units (not bytes)
+4  N bytes  raw       UTF-16LE encoded characters  (N = char_count × 2)
```

For `NameP2K`, the string content is the TCP port number in decimal ASCII,
e.g. `"1999"` → `char_count=4`, 8 bytes of UTF-16LE.
Fallback TCP port when `NameP2K` is empty or non-numeric: **1999**.

### 4.2 Type BA — Byte Array

```
+0  2 bytes  BE WORD   byte_count   Number of following raw bytes
+2  N bytes  raw       payload data
```

IPv4 addresses use `byte_count = 4`; MAC addresses use `byte_count = 6`.

### 4.3 Type W — 16-bit Word

```
+0  2 bytes  BE WORD   value
```

### 4.4 Type DW — 32-bit Double Word

```
+0  4 bytes  BE DWORD  value
```

---

## 5. TCP Transport — Command Sessions

### 5.1 Connection

The host opens a standard TCP connection to the device IP on the TCP port
advertised in `NameP2K` (default 1999), then sends `FUNC_CONNECT`.  The
device responds with `FUNC_ACK` carrying an assigned `session_id`.

### 5.2 TCP Frame Header (20 bytes, big-endian)

```
Offset  Size   Field        Description
──────  ─────  ───────────  ──────────────────────────────────────────────
+0x00   DWORD  magic        0x0000072D
+0x04   WORD   func_code    Command / response code
+0x06   WORD   api_version  Protocol version
+0x08   DWORD  session_id   Assigned by device on FUNC_CONNECT
+0x0C   DWORD  payload_len  Number of bytes following this header
+0x10   DWORD  seq_num      Monotonically increasing per session
```

Total header size: **20 bytes**.

### 5.3 Function Codes

| Code     | Name              | Direction      | Description                    |
|----------|-------------------|----------------|--------------------------------|
| `0x0001` | FUNC_CONNECT      | host → device  | Open session                   |
| `0x0002` | FUNC_DISCONNECT   | host → device  | Close session                  |
| `0x0010` | FUNC_GET_IMAGE    | host → device  | Request image buffer            |
| `0x0020` | FUNC_SET_PARAM    | host → device  | Write configuration parameter   |
| `0x0021` | FUNC_GET_PARAM    | host → device  | Read configuration parameter    |
| `0x0030` | FUNC_STATUS       | host → device  | Query device ready state        |
| `0x0040` | FUNC_TRIGGER      | host → device  | Send X-ray trigger              |
| `0x8000` | FUNC_DISCOVER     | broadcast      | UDP device discovery            |
| `0xFF00` | FUNC_ACK          | device → host  | Generic acknowledgement         |
| `0xFF01` | FUNC_ERROR        | device → host  | Error response                  |

### 5.4 Error Frame Payload

When the device returns `FUNC_ERROR`, the payload starts with a 2-byte BE
WORD error code:

| Code     | Meaning            |
|----------|--------------------|
| `0x0000` | OK (no error)      |
| `0x0001` | Unknown command    |
| `0x0002` | Invalid parameter  |
| `0x0003` | Device busy        |
| `0x0004` | Not ready          |
| `0x0005` | Timeout            |
| `0x0006` | Hardware error     |
| `0x0007` | Access denied      |
| `0x00FF` | Generic error      |

---

## 6. Parameter Map (ORTHOPHOS XG)

These parameter IDs were mapped by capturing Sidexis ↔ ORTHOPHOS XG traffic.

| Param ID | Name              | Type | Notes                           |
|----------|-------------------|------|---------------------------------|
| `0x0010` | kV                | W    | Tube voltage (60–90 kV)         |
| `0x0011` | mA (×10)          | W    | Tube current in tenths of mA    |
| `0x0012` | Exposure time     | W    | Duration in milliseconds        |
| `0x0020` | Program           | W    | Examination program code        |
| `0x0021` | Patient size      | W    | 0=Child, 1=Adult-S/M/L presets  |
| `0x0030` | Rotation speed    | W    | Gantry speed (device units)     |
| `0x0040` | Focus             | W    | 0=Large, 1=Small focal spot     |
| `0x0050` | Layer             | W    | Tomographic layer width         |

### 6.1 Examination Program Codes

| Code   | Program                  |
|--------|--------------------------|
| `0x01` | Panoramic                |
| `0x02` | Cephalometric Lateral    |
| `0x03` | Cephalometric Frontal    |
| `0x10` | Bitewing Left            |
| `0x11` | Bitewing Right           |
| `0x12` | Bitewing Bilateral       |

---

## 7. Typical Session Flow

```
HOST                              DEVICE
 │                                  │
 │── UDP broadcast probe ──────────►│  (FUNC_DISCOVER, port 1999)
 │◄─ UDP response ─────────────────│  (IP, MAC, tcp_port, device_type, …)
 │                                  │
 │── TCP SYN ──────────────────────►│  (connect to advertised tcp_port)
 │◄─ TCP SYN-ACK ──────────────────│
 │                                  │
 │── FUNC_CONNECT ─────────────────►│
 │◄─ FUNC_ACK (session_id=N) ──────│
 │                                  │
 │── FUNC_STATUS ──────────────────►│
 │◄─ FUNC_ACK (status=0x0000) ─────│  (device ready)
 │                                  │
 │── FUNC_SET_PARAM (kV=75) ───────►│
 │◄─ FUNC_ACK ─────────────────────│
 │                                  │
 │── FUNC_TRIGGER ─────────────────►│  (start X-ray exposure)
 │◄─ FUNC_ACK ─────────────────────│
 │                                  │
 │── FUNC_GET_IMAGE ───────────────►│
 │◄─ FUNC_ACK (image bytes…) ──────│
 │                                  │
 │── FUNC_DISCONNECT ──────────────►│
 │◄─ FUNC_ACK ─────────────────────│
 │── TCP FIN ──────────────────────►│
```

---

## 8. Implementation Notes

- The **probe packet** must be sent as a **UDP broadcast** (`SO_BROADCAST`);
  devices do not respond to unicast probes.
- `payload_len` in the UDP header counts from `func_code` (+0x04), not from
  the start of the frame — this is an off-by-4 relative to a naive reading.
- The TCP header is **big-endian** even though the UDP header is
  **little-endian**.  This asymmetry is confirmed.
- `NameP2K` in the discovery response carries the TCP port as a decimal
  ASCII string encoded in **UTF-16LE** (not UTF-8).  Always fall back to
  port 1999 when parsing fails.
- On Windows, Sidexis binds UDP port 1999 exclusively.  PureXS defaults to
  listening on port 55999 to avoid privilege and exclusivity conflicts.
