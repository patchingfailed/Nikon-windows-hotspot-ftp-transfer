# CLAUDE.md

Notes for Claude Code in this repo. Personal details (hardware, network names, working style) live in
`CLAUDE.local.md`, which is not committed.

## Project

Transfers images over Wi-Fi FTP from a Nikon camera (tested: Z8) to a Windows laptop.
The laptop opens a 5 GHz hotspot, the camera joins it in station mode and uploads via FTP.
A local web dashboard shows throughput, connection chain, channel usage, progress and hints.

| File | Content |
|---|---|
| `nikon_ftp_transfer.py` | everything in one file: config, FTP server, file checks, progress, PowerShell script (`PS_SCRIPT`), threads, HTTP API, translation layer (`tr()`) |
| `ui.html` | dashboard, vanilla JS, polls `/api/state` every second, actions via POST `/api/action`, texts via `t()` from `/api/i18n` |
| `locales/en.json`, `locales/de.json` | all user-visible texts (flat key/value, same keys in both files), bundled into the exe |
| `tests/test_progress.py` | tests for image numbers and progress, run without Windows |
| `tests/test_i18n.py` | translation tests: key and placeholder parity, used keys exist, `nikon_ftp_transfer.py` stays ASCII |
| `tests/test_release.py` | `APP_VERSION` format and `APP_CODENAME` match `.github/release-names.json` |
| `.github/workflows/release.yml` | "Build Windows exe": tests, PyInstaller, smoke test, release on tag `v*` |
| `.github/release-names.json` | release code names (see Releases) |
| `config.example.json` | template; the real `config.json` sits next to the program/exe and is gitignored |

## Architecture

- **FTP server** (pyftpdlib) binds only to the hotspot IP `192.168.137.1`, port `2121`,
  PASV `40000–40100`. If the IP is missing, `ftp_loop` waits and rebinds (`S.rebind`).
- **Receiving**: files arrive as `<name>.part`. `finalize()` checks and renames:
  - duplicates by content hash (`quick_hash`: size + first/last MB) against the index of the target folder
  - a shorter partial copy of an existing file is discarded, a longer version replaces a truncated one
  - same name with different content is saved as `_2`, `_3` …
  - `verify()`: NEF/TIFF via the IFD structure (strips, tiles, JPEG preview, SubIFDs), JPEG via `FFD8`/`FFD9`, MOV/MP4 via `ftyp`
- **Supervisor thread** calls the PowerShell script (`Net.run("status")`) every `status_interval` s,
  disconnects the hotspot card from foreign Wi-Fi networks (`prepare`, profile set to `connectionmode=manual`)
  and restarts the hotspot via WinRT (`NetworkOperatorTetheringManager`) when it is off.
- **Scanner thread** scans channels via the second Wi-Fi adapter (USB stick, `uplink_adapter`):
  `WiFiAdapter.ScanAsync()` for a fresh scan, evaluated via `netsh wlan show networks mode=bssid`.
- **Sampler thread** measures throughput every second (including growing `.part` files).
- **Keep-awake thread**: `SetThreadExecutionState` applies per thread, hence a dedicated long-running thread.
  Lid mode via `powercfg /setacvalueindex … LIDACTION`, original values in `_transfer/lid_state.json`.
- **Recovery**: paused Wi-Fi profiles are listed in `_transfer/network_state.json` and reset to
  `connectionmode=auto` on exit. `cleanup()` runs on "Quit", Ctrl+C and via the console ctrl handler
  (closing the window, logoff, shutdown).
- Logs and diagnostic reports live in `<target folder>/_transfer/`.

## Conventions

- Source, comments, logs, docs and commits are English. All user-visible dashboard/message text goes
  through `locales/*.json`: Python `tr()`, `event(level, key, **params)`, hints `add(level, key, ...)`,
  and `t()` in `ui.html`. Add new keys to BOTH locale files. `tests/test_i18n.py` enforces key and
  placeholder parity and that `nikon_ftp_transfer.py` stays ASCII.
- PowerShell script: English, ASCII only (it runs through the console). `Say` emits `ps.*` keys.
- `netsh` output is localized: always write regexes for German and English
  (`Kanal|Channel`, `Funktyp|Radio type` …).
- The PowerShell script prints exactly one JSON line; Python takes the last line that starts with `{`.
- The repo is public: no personal data, names, network names (SSIDs), local paths or
  employer references in committed files. Keep example SSIDs neutral (`Hotel-Guest`, `nikon-ftp`).
  This includes images: zoom into photos/screenshots before committing (browser tabs, bookmarks,
  profile buttons, SSIDs in the dashboard, notifications) and blur what is readable. Check EXIF too.
- GitHub Actions on Windows: a multi-line `run:` in pwsh only fails on the LAST command's exit code.
  Use `shell: bash` (runs with `-e`) or one command per step when every command must succeed.

## Releases

- Bump `APP_VERSION` and `APP_CODENAME` in `nikon_ftp_transfer.py`, push, then tag `vX.Y.Z`.
  The workflow refuses a tag that does not match `APP_VERSION`.
- Code names: the major version picks a family of large sea animals, the minor version a member
  (1.x rays: 1.0 Manta, 1.1 Mobula …; 2.x sharks; 3.x whales …). Patch releases keep the name.
  The list is `.github/release-names.json`; only append, never reorder. The release title becomes
  "vX.Y.Z <name>".

## Testing

```
python tests/test_progress.py                         # tests, any OS
python tests/test_i18n.py                             # translation tests, any OS
python tests/test_release.py                          # version/code name consistency
NFT_TEST=1 NFT_DEMO=1 python nikon_ftp_transfer.py    # dashboard with invented data, any OS
python nikon_ftp_transfer.py --check <folder>         # verify existing NEF/JPEG files
```

- `NFT_TEST=1` (or non-Windows): no network control, FTP on `127.0.0.1` (or `NFT_IP`),
  `NET` is `None`. Further variables: `NFT_TARGET`, `NFT_CONFIG`, `NFT_NO_BROWSER`, `NFT_LANG`.
- The PowerShell/WinRT parts, `powercfg` and `SetThreadExecutionState` only run on real Windows
  and cannot be tested automatically. Keep pure logic (parsing, decisions) in Python functions
  that can be tested with sample data.
- Add new tests as `test_*` functions in `tests/`; the files also run without pytest.

## Findings from field tests

- **Err.1F**: The hotspot runs on the internal card. If that card is also connected to another Wi-Fi,
  the hotspot has to use its channel, and the camera reports `Err.1F`. That is why the tool
  disconnects the card and pauses auto-connect for the profile.
- Windows only starts the hotspot when there is a connection to share (second adapter, LAN
  or the Microsoft KM-TEST Loopback Adapter).
- Windows hides its own hotspot from scans; its channel is therefore usually unknown.
- Windows has no API to set the hotspot channel.
  `NetworkOperatorTetheringAccessPointConfiguration` only knows the band (2.4/5/6 GHz or auto).
- While the hotspot is active, the virtual Wi-Fi Direct adapter ("LAN-Verbindung* N", as Windows
  names it on a German system) shows up as a Wi-Fi adapter (`NdisPhysicalMedium` 9). It must not be
  detected as the stick; `Get-Adapters` therefore filters for adapters known to
  `netsh wlan show interfaces` and for "Wi-Fi Direct|Virtual".
- Port 21 is often taken already (e.g. an old FTP server), hence 2121. Windows sometimes reserves
  TCP 50000–50059 (`netsh interface ipv4 show excludedportrange protocol=tcp`), hence PASV 40000–40100.
- In access-point mode the Z8 only offered 2.4 GHz (72 Mbit/s link, very slow). The right way is
  station mode with "Router frequency band 5 GHz".
- Measured with a Z8 and 64 MB NEFs: about 13–14 MB/s on the 5 GHz hotspot with a free channel.

## Open points

- Never verified on real Windows: automatic choice of the hotspot card (internal instead of USB),
  password display in the camera profile, fresh channel scan via the stick, keep-awake
  (`powercfg /requests`), lid mode without admin rights, recovery after a crash,
  progress display with all three input modes during a real upload.
- Speed varies (7–14 MB/s). Suspicion: hotspot and stick share a channel or channel width.
