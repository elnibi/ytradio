# Project Configuration

## Verification Commands
- Lint: `ruff check . --ignore BLE001,S110,DTZ005,SIM115`
- Typecheck: `mypy . --ignore-missing-imports`
- Test: `python -m unittest -v test_ytradio`
- Format: `ruff format .`

## Dependencies
- Python project with ytmusicapi and yt-dlp
- Install: `pip install -r requirements.txt`

## Code Preferences
- Follow existing code patterns in ytradio.py
- Use existing libraries only (ytmusicapi, yt-dlp)
- Compact code, no unnecessary comments

## Lint Ignorances (Intentional)
- BLE001: Broad Exception catches for error resilience
- S110: try-except-pass for cleanup operations
- DTZ005: datetime.now() without timezone (local time)
- SIM115: Manual file handling for Windows named pipes

## Notes
- Camofox disabled (requires Visual Studio Build Tools)
- Project uses ytmusicapi + yt-dlp, no browser needed
