# MiCast dependency ownership

MiCast owns Xiaomi cloud transport, MIoT signing, speaker command payloads,
model routing, credential recovery and encrypted persistence. These live in
`micast/xiaomi/`. Cloud errors carry safe status/code metadata and do not
trigger a password login or erase credentials. `XiaomiAuth` decides recovery.
An explicit account rate limit is inconclusive, preserves credentials and
enters a two-hour in-memory cooldown after five rate-limit responses.

Device lists populate the model cache; a renewed service receives the current
cached devices. OH2/OH2P prefer `player_play_music`; LX06 prefers
`player_play_url` unless track metadata needs the music command. The alternate
command remains available after a failure. This is cloud control, not proof
of local URL playback support. New protocol routing still requires real-device
regression on OH2, OH2P and LX06 before releasing an installation package.

## Removed dependencies

- `miservice-fork`: replaced by the owned cloud client. Its transitive CLI,
  random-user-agent and QR-reading utilities are no longer needed.
- `aiofiles`: no direct application use; standard-library file persistence
  remains in `config_store.py`.
- `pycryptodome`: classic RAOP AES-CBC and RSA-OAEP now use `cryptography`.
  Packet-tail handling and the legacy AirPlay challenge format are preserved.
- `lucide` and `simple-icons`: existing selected assets are fixed in
  `web/src/icon-data.ts`; licenses and provenance are in `licenses/icons/`.
- Obsolete Windows collection of `zxing_cpp` and fnOS pruning of MiService
  CLI dependencies were removed from packaging.

`aiohttp` is now an explicit runtime requirement; it previously arrived through
MiService. Keep `requirements.txt` and `pyproject.toml` synchronized.

## Retained foundations

- FastAPI/Starlette/Uvicorn, Pydantic/settings and multipart uploads: retain
  the server, validation and upload implementations.
- PyAV/FFmpeg and NumPy: retain native codecs, filters and vectorized audio
  processing. Rewriting these in Python would risk realtime performance.
- Cryptography and Zeroconf: retain encryption and mDNS implementations.
- qrcode/Pillow: retain QR rendering. Pillow also serves desktop tray assets.
- python-dotenv: retain existing environment-file semantics.
- aiohttp and httpx: retain both for now. Cloud sessions use MiCast's bounded
  resolver; DLNA, notifications and orchestration have existing httpx clients
  and tests. Unifying them is an independent transport migration, not required
  to remove MiService. httpx also remains a test dependency.
- pywebview/pystray/pywin32 remain desktop optional dependencies; Docker SDK
  remains specific to orchestration/dev. Avoid including those in plain NAS
  runtime requirements.
- AirPlay 2 continues to use shairport-sync/nqptp and its native runtime;
  classic RAOP and DLNA protocol logic remain owned by MiCast.
- TypeScript/Vite, Playwright/pytest/ruff, Hatchling/PyInstaller/Inno Setup and
  fnpack remain build, test or packaging tools. UPX remains optional.

## Maintenance

Track upstream Xiaomi authentication/signing changes, model-specific protocol
fixes and security corrections. Adopt changes selectively after wire-contract
tests and hardware checks. Keep foundational libraries maintained rather than
copying their implementations into MiCast. Upstream Xiaomi references and
license notices are retained in `licenses/xiaomi/`.
