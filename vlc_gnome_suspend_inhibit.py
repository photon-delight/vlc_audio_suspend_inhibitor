#!/usr/bin/env python3

import gi
gi.require_version('Gio', '2.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gio, GLib
import os
import signal
import logging
import time
import sys
import subprocess # Needed to run gsettings

# --- Configuration ---
LOG_LEVEL = logging.INFO
VLC_DISCOVERY_INTERVAL_SECONDS = 10

# --- D-Bus Constants ---
MPRIS_SERVICE_PREFIX = 'org.mpris.MediaPlayer2.vlc'
MPRIS_OBJECT_PATH = '/org/mpris/MediaPlayer2'
MPRIS_PLAYER_INTERFACE = 'org.mpris.MediaPlayer2.Player'
DBUS_PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'
MPRIS_PLAYBACK_STATUS_PROPERTY = 'PlaybackStatus'
PROPERTIES_CHANGED_SIGNAL = 'PropertiesChanged'
EXPECTED_SIGNAL_PARAM_TYPE = '(sa{sv}as)'

# --- gsettings Constants ---
GSETTINGS_SCHEMA = "org.gnome.settings-daemon.plugins.power"
# We no longer modify these timeout keys, but keep them for reference if needed
# GSETTINGS_KEY_AC_TIMEOUT = "sleep-inactive-ac-timeout"
# GSETTINGS_KEY_BATTERY_TIMEOUT = "sleep-inactive-battery-timeout"
GSETTINGS_KEY_AC_TYPE = "sleep-inactive-ac-type"
GSETTINGS_KEY_BATTERY_TYPE = "sleep-inactive-battery-type"

ENABLED_SLEEP_TYPE = "suspend" # When VLC is not playing, allow suspend
DISABLED_SLEEP_TYPE = "nothing" # When VLC is playing, disable suspend

# --- Global State ---
# Flag to track if we modified settings (specifically the sleep type)
# This helps ensure we only restore if we were the ones who changed it.
settings_changed_by_script = False

session_bus = None
vlc_proxy = None
vlc_service_name = None
main_loop = None
discovery_timer_id = None
signal_subscription_id = None

# --- Logging Setup ---
logging.basicConfig(level=LOG_LEVEL,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# --- gsettings Control ---

def set_gsetting_value(key, value):
    """Helper function to run gsettings set commands."""
    command = ['gsettings', 'set', GSETTINGS_SCHEMA, key, str(value)]
    logging.debug(f"Running command: {' '.join(command)}")
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')
        logging.debug(f"gsettings set {key} {value} successful.")
        return True
    except FileNotFoundError:
        logging.error("Error: 'gsettings' command not found. Is GNOME installed correctly?")
        return False
    except subprocess.CalledProcessError as e:
        logging.error(f"Error running gsettings set {key} {value}: {e}")
        logging.error(f"Stderr: {e.stderr.strip()}")
        return False
    except Exception as e:
        logging.error(f"Unexpected error running gsettings set {key} {value}: {e}", exc_info=True)
        return False

def disable_gnome_autosuspend():
    """Disables GNOME autosuspend by setting sleep type to 'nothing'."""
    global settings_changed_by_script

    logging.info("Attempting to disable GNOME autosuspend (setting type to 'nothing')...")

    ac_type_disabled = set_gsetting_value(GSETTINGS_KEY_AC_TYPE, DISABLED_SLEEP_TYPE)
    battery_type_disabled = set_gsetting_value(GSETTINGS_KEY_BATTERY_TYPE, DISABLED_SLEEP_TYPE)

    if ac_type_disabled and battery_type_disabled:
        logging.info("Successfully disabled GNOME autosuspend (set type to 'nothing').")
        settings_changed_by_script = True
    else:
        logging.error("Failed to fully disable GNOME autosuspend by setting sleep type.")
        # If even one succeeded, we should probably try to restore later
        if ac_type_disabled or battery_type_disabled:
            settings_changed_by_script = True


def restore_gnome_autosuspend():
    """Restores GNOME autosuspend by setting sleep type to 'suspend'."""
    global settings_changed_by_script

    if not settings_changed_by_script:
        logging.debug("Sleep type was not changed by script, nothing to restore.")
        return

    logging.info(f"Attempting to restore autosuspend (setting type to '{ENABLED_SLEEP_TYPE}')...")

    ac_type_restored = set_gsetting_value(GSETTINGS_KEY_AC_TYPE, ENABLED_SLEEP_TYPE)
    battery_type_restored = set_gsetting_value(GSETTINGS_KEY_BATTERY_TYPE, ENABLED_SLEEP_TYPE)

    if ac_type_restored and battery_type_restored:
        logging.info("Successfully restored GNOME autosuspend (set type to 'suspend').")
    else:
        logging.warning("One or more gsettings for sleep type failed to restore. Check logs above.")

    settings_changed_by_script = False # Reset flag after attempting restore
    logging.debug("Reset settings_changed_by_script flag.")


# --- MPRIS Monitoring ---
def on_properties_changed_signal(connection, sender_name, object_path, interface_name, signal_name, parameters):
    """Handles the PropertiesChanged signal received directly from the bus."""
    global vlc_proxy, vlc_service_name # vlc_proxy for is_proxy_valid

    logging.debug(f"--- D-Bus signal received ---")
    logging.debug(f"  Sender: {sender_name}")
    logging.debug(f"  Object Path: {object_path}")
    logging.debug(f"  Interface: {interface_name}")
    logging.debug(f"  Signal Name: {signal_name}")

    if signal_name != PROPERTIES_CHANGED_SIGNAL:
         logging.debug(f"Ignoring signal '{signal_name}' (expected '{PROPERTIES_CHANGED_SIGNAL}').")
         return

    if not vlc_service_name:
        logging.debug("Ignoring signal because no VLC service name is currently tracked.")
        return

    try:
        if parameters and parameters.get_type_string() == EXPECTED_SIGNAL_PARAM_TYPE:
            interface_that_changed, changed_properties_dict, invalidated_properties = parameters.unpack()

            logging.debug(f"  Interface Changed: {interface_that_changed}")
            changed_keys = list(changed_properties_dict.keys()) if changed_properties_dict else []
            logging.debug(f"  Changed Properties ({len(changed_keys)}): {changed_keys}")
            logging.debug(f"  Invalidated Properties: {invalidated_properties}")
            logging.debug(f"-----------------------------")

            if interface_that_changed != MPRIS_PLAYER_INTERFACE:
                logging.debug(f"Ignoring property change for interface: {interface_that_changed} (Expected: {MPRIS_PLAYER_INTERFACE})")
                return

            if not is_proxy_valid(vlc_proxy): # Check if our VLC proxy is still good
                logging.warning("VLC D-Bus proxy became invalid.")
                handle_vlc_disappearance()
                return

            if MPRIS_PLAYBACK_STATUS_PROPERTY in invalidated_properties:
                logging.warning(f"PlaybackStatus property invalidated for {vlc_service_name or 'VLC'}.")
                handle_vlc_disappearance()
                return

            if changed_properties_dict and MPRIS_PLAYBACK_STATUS_PROPERTY in changed_properties_dict:
                status_value = changed_properties_dict[MPRIS_PLAYBACK_STATUS_PROPERTY]
                new_status = None

                if isinstance(status_value, str):
                    new_status = status_value
                elif isinstance(status_value, GLib.Variant) and status_value.get_type_string() == 's':
                    new_status = status_value.get_string()
                else:
                    type_str = status_value.get_type_string() if isinstance(status_value, GLib.Variant) else type(status_value).__name__
                    logging.warning(f"PlaybackStatus changed, but received unexpected value type: {type_str}")

                if new_status:
                    logging.info(f"VLC PlaybackStatus changed to: {new_status}")
                    update_autosuspend_state(new_status)
            elif changed_properties_dict:
                logging.debug(f"Properties changed for {interface_that_changed}, but '{MPRIS_PLAYBACK_STATUS_PROPERTY}' not in the changed set.")
        else:
            param_type_str = parameters.get_type_string() if parameters else 'None'
            logging.warning(f"Received PropertiesChanged signal with unexpected parameters type: {param_type_str} (Expected: {EXPECTED_SIGNAL_PARAM_TYPE})")
    except Exception as e:
        logging.error(f"Error processing PropertiesChanged signal: {e}", exc_info=True)


def update_autosuspend_state(playback_status):
    """Disables or restores GNOME autosuspend based on playback status."""
    if playback_status == "Playing":
        logging.debug("Status is Playing: Disabling GNOME autosuspend.")
        disable_gnome_autosuspend()
    elif playback_status in ["Paused", "Stopped"]:
        logging.debug("Status is Paused/Stopped: Restoring GNOME autosuspend.")
        restore_gnome_autosuspend()
    else:
        logging.info(f"PlaybackStatus is '{playback_status}' (unknown). Restoring GNOME autosuspend for safety.")
        restore_gnome_autosuspend()

# --- D-Bus Helper Functions ---
def get_initial_playback_status(prop_proxy):
    """Gets the initial playback status when connecting to VLC."""
    if not prop_proxy:
        logging.error("get_initial_playback_status called with invalid proxy.")
        return None
    try:
        logging.debug(f"Getting initial PlaybackStatus on interface {MPRIS_PLAYER_INTERFACE}.")
        prop_variant = prop_proxy.call_sync(
            'Get', GLib.Variant('(ss)', (MPRIS_PLAYER_INTERFACE, MPRIS_PLAYBACK_STATUS_PROPERTY)),
            Gio.DBusCallFlags.NONE, -1, None)
        if prop_variant:
            unpacked_result = prop_variant.unpack()
            if unpacked_result:
                result_value = unpacked_result[0]
                status = None
                if isinstance(result_value, str): status = result_value
                elif isinstance(result_value, GLib.Variant) and result_value.get_type_string() == 'v':
                    inner_variant = result_value.get_variant()
                    if inner_variant.get_type_string() == 's': status = inner_variant.get_string()
                elif isinstance(result_value, GLib.Variant) and result_value.get_type_string() == 's':
                     status = result_value.get_string()

                if status:
                    logging.info(f"Initial VLC PlaybackStatus (sync Get): {status}")
                    return status
                logging.warning(f"GetProperty: Unexpected result type {type(result_value)}")
            else: logging.warning("GetProperty returned empty tuple.")
        else: logging.warning("GetProperty call returned None.")
    except Exception as e:
        logging.error(f"Error getting initial PlaybackStatus: {e}", exc_info=True)
    return None

def is_proxy_valid(proxy):
    if not proxy: return False
    try:
        return proxy.get_name_owner() is not None
    except: return False

def handle_vlc_disappearance():
    global vlc_proxy, vlc_service_name, discovery_timer_id
    if vlc_service_name is None: return
    logging.info(f"Handling disappearance of VLC ({vlc_service_name}).")
    restore_gnome_autosuspend()
    vlc_service_name = None
    vlc_proxy = None
    if discovery_timer_id:
        GLib.source_remove(discovery_timer_id)
        discovery_timer_id = None
    start_discovery_timer()

def find_vlc_mpris_service():
    if not session_bus: return None
    try:
        result = session_bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                     'org.freedesktop.DBus', 'ListNames', None,
                                     GLib.VariantType.new('(as)'), Gio.DBusCallFlags.NONE, -1, None)
        if result:
            for name in result.unpack()[0]:
                if name.startswith(MPRIS_SERVICE_PREFIX):
                    logging.info(f"Found VLC MPRIS service: {name}")
                    return name
    except Exception as e:
        logging.error(f"Error listing D-Bus names: {e}")
    return None

def connect_to_vlc():
    global vlc_proxy, vlc_service_name, discovery_timer_id
    if discovery_timer_id:
        GLib.source_remove(discovery_timer_id)
        discovery_timer_id = None
    if vlc_proxy and is_proxy_valid(vlc_proxy): return True

    current_vlc_service_name = find_vlc_mpris_service()
    if current_vlc_service_name:
        logging.info(f"Found VLC: {current_vlc_service_name}. Creating proxy...")
        try:
            new_proxy = Gio.DBusProxy.new_sync(session_bus, Gio.DBusProxyFlags.NONE, None,
                                             current_vlc_service_name, MPRIS_OBJECT_PATH,
                                             DBUS_PROPERTIES_INTERFACE, None)
            vlc_proxy = new_proxy
            vlc_service_name = current_vlc_service_name
            initial_status = get_initial_playback_status(vlc_proxy)
            if initial_status:
                update_autosuspend_state(initial_status)
            else:
                logging.warning("Could not get initial VLC status.")
                restore_gnome_autosuspend() # Ensure restored if initial state unknown
            return True
        except Exception as e:
            logging.error(f"Failed to create proxy for {current_vlc_service_name}: {e}")
            vlc_proxy = None; vlc_service_name = None
    start_discovery_timer()
    return False

def start_discovery_timer():
    global discovery_timer_id
    if discovery_timer_id is None and vlc_service_name is None:
        logging.info(f"Starting VLC discovery timer ({VLC_DISCOVERY_INTERVAL_SECONDS}s).")
        discovery_timer_id = GLib.timeout_add_seconds(VLC_DISCOVERY_INTERVAL_SECONDS, periodic_vlc_check)

def periodic_vlc_check():
    global discovery_timer_id
    if connect_to_vlc():
        discovery_timer_id = None; return False # Stop timer
    return True # Keep timer running

# --- Main Execution & Cleanup ---
def cleanup_handler(signum, frame):
    logging.info(f"Received signal {signum}, shutting down gracefully.")
    global discovery_timer_id, signal_subscription_id, session_bus
    if discovery_timer_id: GLib.source_remove(discovery_timer_id); discovery_timer_id = None
    if signal_subscription_id and session_bus:
        try: session_bus.signal_unsubscribe(signal_subscription_id)
        except: pass # Ignore errors during shutdown
        signal_subscription_id = None
    restore_gnome_autosuspend()
    if main_loop and main_loop.is_running(): main_loop.quit()
    logging.info("Shutdown complete.")

def initial_vlc_connect_attempt():
    connect_to_vlc()
    return GLib.SOURCE_REMOVE

def main():
    global session_bus, main_loop, signal_subscription_id
    logging.info("Starting VLC Playback Inhibit Monitor (gsettings type-only)...")
    signal.signal(signal.SIGINT, cleanup_handler)
    signal.signal(signal.SIGTERM, cleanup_handler)

    try:
        session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        logging.info("Successfully connected to session D-Bus.")
    except Exception as e:
        logging.critical(f"Cannot connect to session D-Bus: {e}", exc_info=True); sys.exit(1)

    try:
        logging.info(f"Subscribing to D-Bus signal for VLC properties...")
        signal_subscription_id = session_bus.signal_subscribe(
            None, DBUS_PROPERTIES_INTERFACE, PROPERTIES_CHANGED_SIGNAL,
            MPRIS_OBJECT_PATH, None, Gio.DBusSignalFlags.NONE,
            on_properties_changed_signal)
        if not signal_subscription_id: logging.error("Failed to subscribe to D-Bus signal.")
        else: logging.info(f"Subscribed with ID: {signal_subscription_id}")
    except Exception as e:
        logging.critical(f"Cannot subscribe to D-Bus signal: {e}", exc_info=True); sys.exit(1)

    main_loop = GLib.MainLoop()
    GLib.idle_add(initial_vlc_connect_attempt)

    try:
        logging.info("Starting GLib main loop...")
        main_loop.run()
    except KeyboardInterrupt: logging.info("KeyboardInterrupt received.")
    except Exception as e: logging.error(f"Exception in main loop: {e}", exc_info=True)
    finally:
        logging.info("Main loop finished.")
        cleanup_handler(0, None) # Ensure cleanup runs

if __name__ == "__main__":
    main()
