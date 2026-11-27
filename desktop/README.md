# AI Signal Windows build

From the repository root, build the standalone app with PyInstaller.

~~~powershell
pyinstaller desktop/AISignal.spec --noconfirm
~~~

Run `dist/AISignal/AISignal.exe` to start the local server and open the dashboard in your browser. It uses live RSS by default and keeps its watchlist and archive under `%LOCALAPPDATA%\AISignal\data`.

To create a per-user installer with Inno Setup, compile `desktop/ai_signal_setup.iss` after the PyInstaller build. The output is `desktop/installer_output/AISignalSetup.exe`.
