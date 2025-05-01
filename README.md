# VLC GNOME Suspend Inhibitor

## Description
This Python script prevents system suspend due to user inactivity on GNOME-based Linux desktops (like Ubuntu 24.04) specifically when VLC media player is actively playing media.

## The Purpose
The Ubuntu 24.04 build of VLC 3.0.20 does not have a setting to inhibit suspend during **audio only** playback.
Systemd version 255 (Used by Ubuntu 24.04) has a reported bug where the standard `systemd-logind` inhibitor locks do not reliably prevent the system from suspending due to inactivity configured via the desktop environment's power settings. This script provides a workaround by directly manipulating the relevant GNOME settings using the `gsettings` command-line tool.  

## How It Works
* **Monitors VLC:** It uses D-Bus (via the `gi` Python bindings) to monitor the MPRIS interface of VLC media player, specifically listening for changes in the `PlaybackStatus` property.
* **Disables Suspend:** When playback starts (`PlaybackStatus` changes to "Playing"), the script reads the current GNOME inactivity suspend timeouts (for AC and battery power) using `gsettings`, stores these values, and then sets the timeouts to `0` (disabled).
* **Restores Suspend:** When playback stops or pauses (`PlaybackStatus` changes to "Stopped" or "Paused"), the script restores the previously stored timeout values using `gsettings`, allowing the normal system idle timer to function again.
* **Handles Cleanup:** If the script is stopped gracefully (e.g., via Ctrl+C or `systemctl stop`) or if VLC disappears, it attempts to restore the original `gsettings` values to ensure the system isn't left with suspend disabled.
* **Systemd Service:** It's designed to run as a background systemd user service for seamless operation.

## Prerequisites

* A Linux system with a GNOME-based desktop environment (e.g., Ubuntu 24.04).
* Python 3.
* Python `gi` bindings (GObject Introspection libraries for GLib, Gio). Usually installed by default on GNOME systems.
    * If not, install via your package manager (e.g., `sudo apt install python3-gi gir1.2-glib-2.0`).
* VLC media player installed.
* The `gsettings` command-line tool (part of standard GNOME).

## Installation and Setup

1.  **Save the Script:** Save the Python script (e.g., as `vlc_gnome_suspend_inhibit.py`) to a suitable location, for example, `~/scripts/` or `/usr/local/bin/`.
2.  **Make Executable:** Open a terminal and make the script executable:
    ```bash
    chmod +x /path/to/your/vlc_gnome_suspend_inhibit.py
    ```
    *(Replace `/path/to/your/vlc_gnome_suspend_inhibit.py` with the actual path)*.
3.  **Create Systemd User Service Directory:**
    ```bash
    mkdir -p ~/.config/systemd/user/
    ```
4.  **Create Service File:** Create a file named `vlc-inhibit-monitor.service` in the directory above using your preferred text editor (e.g., `nano`):
    ```bash
    nano ~/.config/systemd/user/vlc-inhibit-monitor.service
    ```
    Paste the following content, **making sure to update the `ExecStart` path**:
    ```ini
    [Unit]
    Description=VLC GNOME Suspend Inhibitor (using gsettings)
    # Start after the graphical session is ready
    After=graphical-session.target
    PartOf=graphical-session.target

    [Service]
    Type=simple
    # *** IMPORTANT: Update this path! ***
    ExecStart=/usr/bin/python3 /path/to/your/vlc_gnome_suspend_inhibit.py
    # Restart the service if it fails
    Restart=on-failure
    RestartSec=5s

    [Install]
    # Make it part of the default user session target
    WantedBy=default.target
    ```
    Save and close the file (e.g., `Ctrl+X`, then `Y`, then `Enter` in nano).
5.  **Enable and Start:**
    ```bash
    systemctl --user enable --now vlc-inhibit-monitor.service
    ```

## Usage

Once the service is enabled and started, it runs automatically in the background when you are logged in. It will monitor VLC and adjust the GNOME power settings as needed.

* **Check Status:**
    ```bash
    systemctl --user status vlc-inhibit-monitor.service
    ```
* **View Logs:**
    ```bash
    journalctl --user -u vlc-inhibit-monitor.service -f
    ```
    *(Press Ctrl+C to stop following)*
* **Stop:**
    ```bash
    systemctl --user stop vlc-inhibit-monitor.service
    ```
* **Start:**
    ```bash
    systemctl --user start vlc-inhibit-monitor.service
    ```
* **Restart (after script update):**
    ```bash
    systemctl --user restart vlc-inhibit-monitor.service
    ```

## Limitations

* **GNOME Specific:** This script relies entirely on `gsettings` and GNOME's specific power setting keys. It will not work on other desktop environments (KDE, XFCE, etc.).
* **Crash Recovery:** If the script crashes hard without triggering the cleanup handler, the GNOME autosuspend settings might be left disabled (`0`). Restarting the service or logging out/in should allow the script to restore the settings when it next detects VLC is stopped.
* **Manual Setting Changes:** If you manually change the GNOME power settings while the script is running and VLC is playing, the script might overwrite your manual changes when VLC stops (it restores the values it originally saved).