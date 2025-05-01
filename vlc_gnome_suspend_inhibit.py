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
# Set to logging.INFO for normal operation, DEBUG for detailed troubleshooting
LOG_LEVEL = logging.INFO
VLC_DISCOVERY_INTERVAL_SECONDS = 10 # How often to check for VLC if not found

# --- D-Bus Constants ---
MPRIS_SERVICE_PREFIX = 'org.mpris.MediaPlayer2.vlc'
MPRIS_OBJECT_PATH = '/org/mpris/MediaPlayer2'
MPRIS_PLAYER_INTERFACE = 'org.mpris.MediaPlayer2.Player'
DBUS_PROPERTIES_INTERFACE = 'org.freedesktop.DBus.Properties'
MPRIS_PLAYBACK_STATUS_PROPERTY = 'PlaybackStatus'
PROPERTIES_CHANGED_SIGNAL = 'PropertiesChanged' # Signal name
EXPECTED_SIGNAL_PARAM_TYPE = '(sa{sv}as)' # Expected type signature for PropertiesChanged parameters

# --- gsettings Constants ---
GSETTINGS_SCHEMA = "org.gnome.settings-daemon.plugins.power"
GSETTINGS_KEY_AC = "sleep-inactive-ac-timeout"
GSETTINGS_KEY_BATTERY = "sleep-inactive-battery-timeout"
DISABLED_TIMEOUT = "0" # Value to disable autosuspend

# --- Global State ---
# Store original gsettings values
original_ac_timeout = None
original_battery_timeout = None
settings_changed_by_script = False # Flag to track if we modified settings

# D-Bus related state
session_bus = None
vlc_proxy = None # Still used to get initial state and check validity
vlc_service_name = None # Still used to know *which* service we found
main_loop = None
discovery_timer_id = None
signal_subscription_id = None # ID for the bus signal subscription

# --- Logging Setup ---
logging.basicConfig(level=LOG_LEVEL,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# --- gsettings Control ---

def run_gsettings(args):
    """Helper function to run gsettings commands."""
    command = ['gsettings'] + args
    logging.debug(f"Running command: {' '.join(command)}")
    try:
        # Run command. Need text=True for string output/input.
        # Capture output to get current values. Check=True raises error on failure.
        result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8')
        logging.debug(f"gsettings command successful. stdout: {result.stdout.strip()}, stderr: {result.stderr.strip()}")
        return result.stdout.strip()
    except FileNotFoundError:
        logging.error("Error: 'gsettings' command not found. Is GNOME installed correctly?")
        return None
    except subprocess.CalledProcessError as e:
        logging.error(f"Error running gsettings {' '.join(args)}: {e}")
        logging.error(f"Stderr: {e.stderr.strip()}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error running gsettings {' '.join(args)}: {e}", exc_info=True)
        return None

def get_current_timeout(key):
    """Gets the current timeout value for a given gsettings key."""
    return run_gsettings(['get', GSETTINGS_SCHEMA, key])

def set_timeout(key, value):
    """Sets the timeout value for a given gsettings key."""
    return run_gsettings(['set', GSETTINGS_SCHEMA, key, str(value)])

def disable_gnome_autosuspend():
    """Disables GNOME autosuspend by setting timeouts to 0."""
    global original_ac_timeout, original_battery_timeout, settings_changed_by_script

    # Only store original values the first time we disable
    if not settings_changed_by_script:
        logging.debug("First time disabling, getting original timeouts.")
        current_ac = get_current_timeout(GSETTINGS_KEY_AC)
        current_battery = get_current_timeout(GSETTINGS_KEY_BATTERY)

        # Check if gsettings calls succeeded
        if current_ac is None or current_battery is None:
            logging.error("Failed to get current gsettings timeouts. Cannot safely disable.")
            return

        original_ac_timeout = current_ac
        original_battery_timeout = current_battery
        logging.info(f"Stored original timeouts: AC={original_ac_timeout}, Battery={original_battery_timeout}")
    else:
        logging.debug("Settings already changed by script, not re-reading originals.")

    # Disable AC timeout if it's not already disabled
    if original_ac_timeout != DISABLED_TIMEOUT:
        logging.info(f"Disabling AC autosuspend (setting {GSETTINGS_KEY_AC} to {DISABLED_TIMEOUT}).")
        if set_timeout(GSETTINGS_KEY_AC, DISABLED_TIMEOUT) is not None:
            settings_changed_by_script = True # Mark that we changed something
        else:
            logging.error("Failed to disable AC timeout.")
    else:
        logging.debug("AC timeout already disabled.")
        # Still mark as changed if we intend to manage it
        settings_changed_by_script = True

    # Disable Battery timeout if it's not already disabled
    if original_battery_timeout != DISABLED_TIMEOUT:
        logging.info(f"Disabling Battery autosuspend (setting {GSETTINGS_KEY_BATTERY} to {DISABLED_TIMEOUT}).")
        if set_timeout(GSETTINGS_KEY_BATTERY, DISABLED_TIMEOUT) is not None:
            settings_changed_by_script = True # Mark that we changed something
        else:
            logging.error("Failed to disable Battery timeout.")
    else:
        logging.debug("Battery timeout already disabled.")
         # Still mark as changed if we intend to manage it
        settings_changed_by_script = True


def restore_gnome_autosuspend():
    """Restores the original GNOME autosuspend timeout values."""
    global original_ac_timeout, original_battery_timeout, settings_changed_by_script

    # Only restore if we actually changed the settings
    if not settings_changed_by_script:
        logging.debug("Settings were not changed by script, nothing to restore.")
        return

    restored_something = False
    logging.info("Attempting to restore original autosuspend timeouts...")

    # Restore AC timeout if we stored an original value
    if original_ac_timeout is not None:
        logging.info(f"Restoring AC timeout to {original_ac_timeout}.")
        if set_timeout(GSETTINGS_KEY_AC, original_ac_timeout) is not None:
            restored_something = True
        else:
            logging.error("Failed to restore AC timeout.")
        original_ac_timeout = None # Clear stored value after attempt
    else:
        logging.debug("No original AC timeout stored.")

    # Restore Battery timeout if we stored an original value
    if original_battery_timeout is not None:
        logging.info(f"Restoring Battery timeout to {original_battery_timeout}.")
        if set_timeout(GSETTINGS_KEY_BATTERY, original_battery_timeout) is not None:
             restored_something = True
        else:
            logging.error("Failed to restore Battery timeout.")
        original_battery_timeout = None # Clear stored value after attempt
    else:
        logging.debug("No original Battery timeout stored.")

    # Reset the flag only if we successfully attempted restoration
    if restored_something:
        settings_changed_by_script = False
    else:
        # If restoration failed, keep the flag true maybe? Or log error.
        logging.warning("Restoration attempt finished, but might not have fully succeeded.")
        # Decide if flag should be reset even on failure. For safety, maybe keep it True?
        # Let's reset it to avoid repeated failed attempts.
        settings_changed_by_script = False


# --- MPRIS Monitoring ---
def on_properties_changed_signal(connection, sender_name, object_path, interface_name, signal_name, parameters):
    """Handles the PropertiesChanged signal received directly from the bus."""
    global vlc_proxy, vlc_service_name

    logging.debug(f"--- D-Bus signal received ---")
    logging.debug(f"  Sender: {sender_name}")
    logging.debug(f"  Object Path: {object_path}")
    logging.debug(f"  Interface: {interface_name}")
    logging.debug(f"  Signal Name: {signal_name}")

    # Check if it's the signal we expect
    if signal_name != PROPERTIES_CHANGED_SIGNAL:
         logging.debug(f"Ignoring signal '{signal_name}' (expected '{PROPERTIES_CHANGED_SIGNAL}').")
         return

    # Check if we have identified a VLC service name.
    if not vlc_service_name:
        logging.debug("Ignoring signal because no VLC service name is currently tracked.")
        return

    # Parameters is a GVariant tuple: (sa{sv}as)
    try:
        if parameters and parameters.get_type_string() == EXPECTED_SIGNAL_PARAM_TYPE:
            # Unpack the tuple
            interface_that_changed, changed_properties_dict, invalidated_properties = parameters.unpack()

            logging.debug(f"  Interface Changed: {interface_that_changed}")
            changed_keys = list(changed_properties_dict.keys()) if changed_properties_dict else []
            logging.debug(f"  Changed Properties ({len(changed_keys)}): {changed_keys}")
            logging.debug(f"  Invalidated Properties: {invalidated_properties}")
            logging.debug(f"-----------------------------")

            # Check if the change happened on the Player interface
            if interface_that_changed != MPRIS_PLAYER_INTERFACE:
                logging.debug(f"Ignoring property change for interface: {interface_that_changed} (Expected: {MPRIS_PLAYER_INTERFACE})")
                return

            # Check proxy validity (still useful to know if VLC is alive)
            if not is_proxy_valid(vlc_proxy):
                logging.warning("Properties changed signal received, but associated proxy object is no longer valid.")
                handle_vlc_disappearance() # Trigger cleanup if proxy is gone
                return

            # Check invalidated properties first
            if MPRIS_PLAYBACK_STATUS_PROPERTY in invalidated_properties:
                logging.warning(f"PlaybackStatus property invalidated for {vlc_service_name or 'VLC'}.")
                handle_vlc_disappearance()
                return

            # Check changed properties
            if changed_properties_dict and MPRIS_PLAYBACK_STATUS_PROPERTY in changed_properties_dict:
                status_value = changed_properties_dict[MPRIS_PLAYBACK_STATUS_PROPERTY]
                new_status = None

                if isinstance(status_value, str):
                    logging.debug("PlaybackStatus value in signal is directly a string.")
                    new_status = status_value
                elif isinstance(status_value, GLib.Variant) and status_value.get_type_string() == 's':
                    logging.debug("PlaybackStatus value in signal is a Variant 's'.")
                    new_status = status_value.get_string()
                else:
                    type_str = status_value.get_type_string() if isinstance(status_value, GLib.Variant) else type(status_value).__name__
                    logging.warning(f"PlaybackStatus changed, but received unexpected value type in dictionary: {type_str}")

                if new_status:
                    logging.info(f"VLC PlaybackStatus changed to: {new_status}")
                    update_autosuspend_state(new_status) # Call the gsettings update function

            elif changed_properties_dict:
                logging.debug(f"Properties changed signal received for {interface_that_changed}, but '{MPRIS_PLAYBACK_STATUS_PROPERTY}' not in the changed set.")

        else:
            logging.warning(f"Received PropertiesChanged signal with unexpected parameters type: {parameters.get_type_string() if parameters else 'None'} (Expected: {EXPECTED_SIGNAL_PARAM_TYPE})")

    except Exception as e:
        logging.error(f"Error processing PropertiesChanged signal parameters: {e}", exc_info=True)


def update_autosuspend_state(playback_status):
    """Disables or restores GNOME autosuspend based on playback status."""
    if playback_status == "Playing":
        logging.debug("Status is Playing: Disabling GNOME autosuspend.")
        disable_gnome_autosuspend()
    elif playback_status in ["Paused", "Stopped"]:
        logging.debug("Status is Paused/Stopped: Restoring GNOME autosuspend.")
        restore_gnome_autosuspend()
    else:
        # For unknown states, restore autosuspend to be safe
        logging.info(f"PlaybackStatus is '{playback_status}' (unknown). Restoring GNOME autosuspend.")
        restore_gnome_autosuspend()

# --- D-Bus Helper Functions ---
# (get_initial_playback_status, is_proxy_valid, handle_vlc_disappearance, find_vlc_mpris_service, connect_to_vlc, start_discovery_timer, periodic_vlc_check)
# These remain largely the same, but handle_vlc_disappearance needs to call restore_gnome_autosuspend

def get_initial_playback_status(prop_proxy):
    """Gets the initial playback status when connecting to VLC."""
    if not prop_proxy:
        logging.error("get_initial_playback_status called with invalid proxy.")
        return None
    try:
        logging.debug(f"Trying synchronous GetProperty for initial PlaybackStatus on interface {MPRIS_PLAYER_INTERFACE}.")
        prop_variant = prop_proxy.call_sync(
            'Get',
            GLib.Variant('(ss)', (MPRIS_PLAYER_INTERFACE, MPRIS_PLAYBACK_STATUS_PROPERTY)),
            Gio.DBusCallFlags.NONE, -1, None)

        if prop_variant:
            unpacked_result = prop_variant.unpack()
            if unpacked_result:
                result_value = unpacked_result[0]
                status = None
                if isinstance(result_value, str):
                    logging.debug("GetProperty result appears to be directly a string.")
                    status = result_value
                elif isinstance(result_value, GLib.Variant) and result_value.get_type_string() == 'v':
                    inner_variant = result_value.get_variant()
                    if inner_variant.get_type_string() == 's':
                        status = inner_variant.get_string()
                    else:
                        logging.warning(f"GetProperty returned variant 'v' containing unexpected type: {inner_variant.get_type_string()}")
                elif isinstance(result_value, GLib.Variant) and result_value.get_type_string() == 's':
                     status = result_value.get_string()
                else:
                    type_str = result_value.get_type_string() if isinstance(result_value, GLib.Variant) else 'N/A'
                    logging.warning(f"GetProperty returned unexpected result type in tuple: {type(result_value)} with type string {type_str}")

                if status:
                    logging.info(f"Initial VLC PlaybackStatus (sync Get): {status}")
                    return status
                else:
                    logging.warning("GetProperty call succeeded but failed to extract status string from result.")
            else:
                logging.warning("GetProperty call returned an empty tuple.")
        else:
            logging.warning("GetProperty call returned None.")

    except GLib.Error as e:
         logging.error(f"Error getting initial PlaybackStatus: {e.dbus_error if hasattr(e, 'dbus_error') and e.dbus_error else e}")
    except Exception as e:
        logging.error(f"Unexpected error getting initial PlaybackStatus: {e}", exc_info=True)

    logging.warning("Could not determine initial PlaybackStatus.")
    return None # Indicate failure

def is_proxy_valid(proxy):
    """Basic check if the proxy seems valid by checking its name owner."""
    if not proxy:
        return False
    try:
        owner = proxy.get_name_owner()
        return owner is not None
    except (GLib.Error, Exception) as e:
        logging.debug(f"Proxy validity check failed: {e}")
        return False

def handle_vlc_disappearance():
    """Handles the case when the VLC proxy becomes invalid or VLC quits."""
    global vlc_proxy, vlc_service_name, discovery_timer_id

    if vlc_service_name is None:
        logging.debug("handle_vlc_disappearance called, but already handled or not connected.")
        return

    logging.info(f"Handling disappearance of VLC ({vlc_service_name}).")

    # Restore GNOME settings if they were changed by the script
    restore_gnome_autosuspend()

    vlc_service_name = None
    vlc_proxy = None

    if discovery_timer_id:
        GLib.source_remove(discovery_timer_id)
        discovery_timer_id = None

    start_discovery_timer()


def find_vlc_mpris_service():
    """Finds the D-Bus service name for a running VLC MPRIS instance."""
    if not session_bus:
        logging.error("Session bus not available for VLC discovery.")
        return None
    try:
        result_variant = session_bus.call_sync(
            'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'ListNames', None, GLib.VariantType.new('(as)'),
            Gio.DBusCallFlags.NONE, -1, None )
        if result_variant:
            names = result_variant.unpack()[0]
            for name in names:
                if name.startswith(MPRIS_SERVICE_PREFIX):
                    logging.info(f"Found VLC MPRIS service: {name}")
                    return name
        else:
            logging.warning("ListNames D-Bus call returned None.")
    except GLib.Error as e:
        logging.error(f"Error listing D-Bus names: {e.dbus_error if hasattr(e, 'dbus_error') and e.dbus_error else e}")
    except Exception as e:
        logging.error(f"Unexpected error listing D-Bus names: {e}")

    logging.debug("No VLC MPRIS service found.")
    return None

def connect_to_vlc():
    """Attempts to find and connect to the VLC MPRIS service. Returns True on success, False on failure."""
    global vlc_proxy, vlc_service_name, discovery_timer_id

    if discovery_timer_id:
        logging.debug("Stopping discovery timer for connection attempt.")
        GLib.source_remove(discovery_timer_id)
        discovery_timer_id = None

    if vlc_proxy and is_proxy_valid(vlc_proxy):
        logging.debug("Already connected to VLC and proxy seems valid.")
        return True

    logging.debug("Attempting to find VLC service...")
    current_vlc_service_name = find_vlc_mpris_service()

    if current_vlc_service_name:
        logging.info(f"Found VLC service: {current_vlc_service_name}. Attempting to create proxy...")
        try:
            new_proxy = Gio.DBusProxy.new_sync(
                session_bus, Gio.DBusProxyFlags.NONE, None,
                current_vlc_service_name, MPRIS_OBJECT_PATH,
                DBUS_PROPERTIES_INTERFACE, None )
            logging.info(f"Successfully created D-Bus proxy for {current_vlc_service_name} ({DBUS_PROPERTIES_INTERFACE})")

            vlc_proxy = new_proxy
            vlc_service_name = current_vlc_service_name

            initial_status = get_initial_playback_status(vlc_proxy)
            if initial_status:
                update_autosuspend_state(initial_status) # Use gsettings update function
            else:
                logging.warning("Could not get initial status, assuming not playing.")
                # Ensure settings are restored if we couldn't determine initial state
                restore_gnome_autosuspend()

            return True

        except GLib.Error as e:
            logging.error(f"Failed to create proxy for {current_vlc_service_name}: {e.dbus_error if hasattr(e, 'dbus_error') and e.dbus_error else e}")
            vlc_proxy = None
            vlc_service_name = None
        except Exception as e:
            logging.error(f"Unexpected error creating proxy for {current_vlc_service_name}: {e}")
            vlc_proxy = None
            vlc_service_name = None

    logging.info("Could not connect to VLC service this time.")
    vlc_proxy = None
    vlc_service_name = None
    start_discovery_timer()
    return False

def start_discovery_timer():
    """Starts the periodic timer to discover VLC if not already running."""
    global discovery_timer_id
    if discovery_timer_id is None and vlc_service_name is None:
        logging.info(f"Starting VLC discovery timer ({VLC_DISCOVERY_INTERVAL_SECONDS}s interval).")
        discovery_timer_id = GLib.timeout_add_seconds(VLC_DISCOVERY_INTERVAL_SECONDS, periodic_vlc_check)
    elif discovery_timer_id is not None:
         logging.debug("Discovery timer already running.")
    elif vlc_service_name is not None:
         logging.debug("Not starting discovery timer as VLC service name is set.")


def periodic_vlc_check():
    """Callback function for the discovery timer. Returns True to keep running, False to stop."""
    global discovery_timer_id
    logging.debug("Periodic VLC check triggered.")
    if connect_to_vlc():
        logging.info("VLC connected via periodic check, stopping discovery timer.")
        discovery_timer_id = None
        return False
    else:
        logging.debug("VLC not found in periodic check, timer continues.")
        return True

# --- Main Execution & Cleanup ---
def cleanup_handler(signum, frame):
    """Signal handler for clean shutdown."""
    logging.info(f"Received signal {signum}, shutting down gracefully.")

    global discovery_timer_id, signal_subscription_id, session_bus
    # Stop discovery timer first
    if discovery_timer_id:
        logging.debug("Removing discovery timer during cleanup.")
        GLib.source_remove(discovery_timer_id)
        discovery_timer_id = None

    # Unsubscribe from D-Bus signal
    if signal_subscription_id and session_bus:
        logging.debug(f"Unsubscribing from bus signal (ID: {signal_subscription_id}).")
        try:
            session_bus.signal_unsubscribe(signal_subscription_id)
        except Exception as e:
            logging.error(f"Error unsubscribing from signal: {e}")
        signal_subscription_id = None

    # Restore GNOME settings if they were changed
    restore_gnome_autosuspend()

    # Quit main loop
    if main_loop and main_loop.is_running():
        logging.info("Quitting main loop...")
        main_loop.quit()
    else:
         logging.info("Main loop not running or already quit.")
    logging.info("Shutdown sequence initiated.")

def initial_vlc_connect_attempt():
    """Calls connect_to_vlc once and returns False to prevent idle_add loop."""
    logging.debug("Performing initial VLC connection attempt...")
    connect_to_vlc()
    return GLib.SOURCE_REMOVE

def main():
    global session_bus, main_loop, signal_subscription_id # Removed systemd_proxy

    logging.info("Starting VLC Playback Inhibit Monitor (using gsettings)...")

    signal.signal(signal.SIGINT, cleanup_handler)
    signal.signal(signal.SIGTERM, cleanup_handler)

    # Connect to Session Bus (Still needed for VLC monitoring)
    try:
        logging.debug("Connecting to Session D-Bus...")
        session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        logging.info("Successfully connected to session D-Bus.")
    except Exception as e:
        logging.critical(f"Cannot connect to session D-Bus: {e}", exc_info=True)
        sys.exit(1)

    # Subscribe to PropertiesChanged signal globally (for VLC status)
    try:
        logging.info(f"Subscribing to D-Bus signal: Interface='{DBUS_PROPERTIES_INTERFACE}', Signal='{PROPERTIES_CHANGED_SIGNAL}', Object Path='{MPRIS_OBJECT_PATH}'")
        signal_subscription_id = session_bus.signal_subscribe(
            None, DBUS_PROPERTIES_INTERFACE, PROPERTIES_CHANGED_SIGNAL,
            MPRIS_OBJECT_PATH, None, Gio.DBusSignalFlags.NONE,
            on_properties_changed_signal
        )
        if signal_subscription_id == 0:
             logging.error("Failed to subscribe to PropertiesChanged signal (returned ID 0).")
        else:
             logging.info(f"Successfully subscribed to PropertiesChanged signal (ID: {signal_subscription_id}).")
    except Exception as e:
         logging.critical(f"Cannot subscribe to PropertiesChanged signal: {e}", exc_info=True)
         sys.exit(1)


    main_loop = GLib.MainLoop()

    # Initial attempt to connect to VLC (creates proxy, gets initial state, sets initial gsettings)
    logging.debug("Scheduling initial VLC connection attempt via idle_add.")
    GLib.idle_add(initial_vlc_connect_attempt)

    # Start the main loop
    try:
        logging.info("Starting GLib main loop. Monitoring VLC...")
        main_loop.run()
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received during main loop.")
    except Exception as e:
        logging.error(f"Unhandled exception escaped main loop: {e}", exc_info=True)
        cleanup_handler(signal.SIGTERM, None)
    finally:
        logging.info("Main loop finished.")
        # Ensure cleanup runs if loop exits unexpectedly
        if signal_subscription_id and session_bus:
             try: session_bus.signal_unsubscribe(signal_subscription_id)
             except: pass
        restore_gnome_autosuspend() # Restore settings on exit
        logging.info("VLC Playback Inhibit Monitor stopped.")


if __name__ == "__main__":
    main()
