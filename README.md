# DeviceWebUI CWC

`DeviceWebUI` is a WinCC Unified Custom Web Control for browsing device links from text files and opening them inside the HMI screen through a local proxy backend.

## Repository Contents

- `deploy_cwc.bat`: builds and copies the CWC ZIP to the WinCC `CustomControls` folder
- `{26B93B78-F241-492A-BCB4-85C36436DEEA}/`: CWC manifest, HTML, CSS, and JavaScript
- `devicewebui_local_server.py`: primary local backend and proxy
- `devicewebui_local_server.ps1`: legacy helper variant
- `start_devicewebui_local_server.bat`: starts the local helper

## What It Does

- reads `.txt` link definitions from a configured root folder
- lists device folders and files in the CWC UI
- resolves the selected file into a target URL
- proxies embedded web UIs that would normally refuse `iframe` embedding
- emits a single string event for the currently selected path

## Expected Link File Format

The control expects text files where:

1. first non-empty line = target URL
2. second non-empty line = icon name

Icons are resolved from the `Icons` folder under the configured root folder.

## Local Development

1. Start the helper with `start_devicewebui_local_server.bat`
2. Deploy the CWC with `deploy_cwc.bat`
3. Reload the WinCC screen or reimport the control if needed

## Notes

- the Python helper is the active backend
- the PowerShell helper is kept only as a fallback/reference
- dynamic public websites are best-effort through the proxy; device UIs are the main target
