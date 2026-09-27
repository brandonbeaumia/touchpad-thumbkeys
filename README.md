# Touchpad ThumbKeys

Converts the top strip of your touchpad into configurable left/right zones that output keycodes,
each zone intended for a different thumb.

## Configure

Before a full installation, configure the daemon:

1. Run the script interactively with debugging enabled:
  `sudo python3 touchpad-thumbkeys.py --debug`
2. Place your fingers on home row and repeatedly tap your touchpad with each thumb. Note the X and Y coordinates that are reported.
3. Press `Ctrl+C` to exit.
4. Open `touchpad-thumbkeys.py` in your text editor and edit the (thorougly-commented) Configuration section to set up your two zones, two keycodes, and a few preferences.
5. Repeat until it is behaving to your liking.

## Install (Optional)

After achieving your preferred settings, you may install the script and systemd service to run it automatically in the background.

    sudo install -m755 touchpad-thumbkeys.py /usr/local/bin/thumbkeys.py
    sudo install -m644 touchpad-thumbkeys.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now touchpad-thumbkeys

## Uninstall

    sudo systemctl disable --now touchpad-thumbkeys
    sudo rm /usr/local/bin/thumbkeys.py /etc/systemd/system/touchpad-thumbkeys.service
    sudo systemctl daemon-reload

## Troubleshooting

1. Stop the background daemon: `sudo systemctl stop touchpad-thumbkeys`
2. Run the installed daemon interactively: `sudo python3 /usr/local/bin/touchpad-thumbkeys.py --debug`
3. Tap the touchpad to see zone evaluations, coordinate tracking, slide-out states, and key emissions. 

## Touchpad Conflict Mitigation

This daemon is designed to not interfere with most regular touchpad movement:
*   **Landing Zone Only:** Sliding into the thumb zones is passed through as regular cursor movement.
*   **Dynamic Slide-Out:** Sliding out of a thumb zone releases the synthetic key and begins touch pass-through.
*   **Same-Zone Cancellation:** If two fingers land in the same thumb zone (e.g., initiating a two-finger scroll near the top edge), keys are released and touch input is passed through.
*   **Exclusive Key Mode:** (optional) Any simultaneous left+right thumb zone enablement releases the keys and begins pass-through (a more eager version of same-zone cancellation).
*   **Hold Time Delay:** (optional) Require that a finger remain in a thumb zone for at least a number of milliseconds before its key is outputted.

## License 

GPL-2.0-only