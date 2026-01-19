#!/usr/bin/env python3
"""
Test script for Fire TV Controller.

Usage:
    # Auto-discover and connect to Fire TV
    python3 test_fire_tv.py

    # Connect to specific IP
    python3 test_fire_tv.py 192.168.1.100

    # Run interactive mode
    python3 test_fire_tv.py --interactive

    # Just scan for devices
    python3 test_fire_tv.py --scan

    # Auto-setup with guided instructions
    python3 test_fire_tv.py --setup
"""

import argparse
import logging
import sys
import time

# Add src to path
sys.path.insert(0, '/home/radxa/Minus')

from src.fire_tv import FireTVController, quick_connect, auto_setup, KEY_CODES

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def scan_devices():
    """Scan network for Fire TV devices."""
    print("\n=== Scanning for Fire TV devices ===\n")
    devices = FireTVController.discover_devices(timeout=10.0)

    if not devices:
        print("No Fire TV devices found.")
        print("\nTroubleshooting:")
        print("1. Make sure ADB debugging is enabled on your Fire TV")
        print("2. Ensure Fire TV is awake (not in sleep mode)")
        print("3. Check that both devices are on the same network")
        return None

    print(f"Found {len(devices)} device(s):\n")
    for i, dev in enumerate(devices, 1):
        hostname = dev.get('hostname') or 'unknown'
        print(f"  {i}. {dev['ip']} ({hostname})")

    return devices


def test_connection(ip_address: str):
    """Test connecting to a Fire TV."""
    print(f"\n=== Testing connection to {ip_address} ===\n")

    controller = FireTVController()

    print("Connecting...")
    if not controller.connect(ip_address):
        print("\nConnection failed!")
        print("\nIf this is your first time connecting:")
        print("1. Look at your TV screen")
        print("2. You should see an 'Allow USB debugging?' dialog")
        print("3. Check 'Always allow from this computer'")
        print("4. Click 'OK'")
        print("5. Then run this script again")
        return None

    print("Connected successfully!")

    # Get device info
    info = controller.get_device_info()
    if info:
        print(f"\nDevice Info:")
        print(f"  Manufacturer: {info.get('manufacturer')}")
        print(f"  Model: {info.get('model')}")
        print(f"  Android: {info.get('android_version')}")
        print(f"  Current App: {info.get('current_app')}")

    return controller


def test_commands(controller: FireTVController):
    """Test sending basic commands."""
    print("\n=== Testing commands ===\n")

    tests = [
        ("Getting current app", lambda: controller.get_current_app()),
        ("Checking connection", lambda: controller.is_connected()),
    ]

    for name, test_fn in tests:
        print(f"  {name}...", end=" ")
        try:
            result = test_fn()
            print(f"OK ({result})")
        except Exception as e:
            print(f"FAILED ({e})")


def interactive_mode(controller: FireTVController):
    """Interactive remote control mode."""
    print("\n=== Interactive Remote Control ===")
    print("\nCommands:")
    print("  Navigation: up, down, left, right, select, back, home")
    print("  Media: play, pause, play_pause, stop, fast_forward, rewind")
    print("  Volume: volume_up, volume_down, mute")
    print("  Other: menu, search, power, wakeup, sleep")
    print("  Special: skip (tries to skip ad), app (show current app)")
    print("  Type 'quit' or 'q' to exit")
    print()

    while True:
        try:
            cmd = input("Remote> ").strip().lower()

            if not cmd:
                continue

            if cmd in ('quit', 'q', 'exit'):
                break

            if cmd == 'skip':
                print("Attempting to skip ad...")
                controller.skip_ad()
            elif cmd == 'app':
                app = controller.get_current_app()
                print(f"Current app: {app}")
            elif cmd == 'status':
                status = controller.get_status()
                print(f"Status: {status}")
            elif cmd == 'info':
                info = controller.get_device_info()
                print(f"Info: {info}")
            elif cmd in KEY_CODES:
                result = controller.send_command(cmd)
                if result:
                    print(f"Sent: {cmd}")
                else:
                    print(f"Failed to send: {cmd}")
            else:
                print(f"Unknown command: {cmd}")
                print(f"Available: {', '.join(sorted(KEY_CODES.keys()))}")

        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except EOFError:
            break

    print("Disconnecting...")
    controller.disconnect()


def demo_sequence(controller: FireTVController):
    """Run a demo sequence of commands."""
    print("\n=== Running Demo Sequence ===")
    print("This will send: home -> wait -> down -> down -> select")
    print("Press Enter to continue or Ctrl+C to cancel...")

    try:
        input()
    except KeyboardInterrupt:
        print("\nCancelled")
        return

    commands = [
        ("Going home", "home"),
        ("Wait", None),  # Just a pause
        ("Navigate down", "down"),
        ("Navigate down", "down"),
        ("Select", "select"),
    ]

    for desc, cmd in commands:
        print(f"  {desc}...", end=" ", flush=True)
        if cmd is None:
            time.sleep(1.0)
            print("OK")
        else:
            if controller.send_command(cmd):
                print("OK")
            else:
                print("FAILED")
            time.sleep(0.5)

    print("\nDemo complete!")


def main():
    parser = argparse.ArgumentParser(
        description="Test Fire TV Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 test_fire_tv.py                    # Auto-discover and connect to Fire TV
  python3 test_fire_tv.py 192.168.1.100     # Connect to specific IP
  python3 test_fire_tv.py --scan             # Just scan for ADB devices
  python3 test_fire_tv.py --setup            # Guided setup with instructions
  python3 test_fire_tv.py --interactive      # Interactive remote mode
  python3 test_fire_tv.py -i 192.168.1.100   # Interactive with specific IP
"""
    )
    parser.add_argument('ip', nargs='?', help='Fire TV IP address')
    parser.add_argument('--scan', '-s', action='store_true', help='Scan for ADB devices only')
    parser.add_argument('--setup', action='store_true', help='Guided auto-setup with instructions')
    parser.add_argument('--interactive', '-i', action='store_true', help='Interactive mode')
    parser.add_argument('--demo', '-d', action='store_true', help='Run demo sequence')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Just scan
    if args.scan:
        devices = scan_devices()
        return 0 if devices else 1

    # Guided setup
    if args.setup:
        controller = auto_setup()
        if controller:
            if args.interactive:
                interactive_mode(controller)
            else:
                print("\nFire TV is ready! Run with --interactive for remote control.")
                controller.disconnect()
            return 0
        return 1

    # Get IP address
    ip_address = args.ip
    if not ip_address:
        # Use smart detection
        print("\n=== Searching for Fire TV ===\n")
        controller = quick_connect()
        if controller:
            # Test basic commands
            test_commands(controller)

            # Demo or interactive mode
            if args.demo:
                demo_sequence(controller)
            elif args.interactive:
                interactive_mode(controller)
            else:
                print("\nConnection test successful!")
                print("\nTry running with --interactive for remote control")
                print("or --demo for a demo sequence")

            controller.disconnect()
            return 0
        return 1

    # Connect
    controller = test_connection(ip_address)
    if not controller:
        return 1

    # Test basic commands
    test_commands(controller)

    # Demo or interactive mode
    if args.demo:
        demo_sequence(controller)
    elif args.interactive:
        interactive_mode(controller)
    else:
        print("\nConnection test successful!")
        print("\nTry running with --interactive for remote control")
        print("or --demo for a demo sequence")

    # Cleanup
    controller.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
