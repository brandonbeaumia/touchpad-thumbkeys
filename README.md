# Touchpad ThumbKeys

Converts the top strip of your touchpad into configurable left/right zones that output keycodes,
each zone intended for a different thumb.

## Install (most mutable distros)

    sudo install -m755 numberpadd.py /usr/local/bin/numberpadd
    sudo install -m644 numberpadd.service /etc/systemd/system/
    sudo systemctl enable --now numberpadd

## Install on Immutable Distro (Bazzite, Silverblue, Kinoite, etc.)

    TODO

## Configure

1. Run `sudo python3 thumbkeys.py --debug` and repeatedly tap your touchpad with each thumb.
2. Set ZONE_HEIGHT slightly higher than the largest reported Y value.
3. Set CENTER_X to a point about halfway between the two X values reported by each thumb.
4. Set KEY_TOP_LEFT and KEY_TOP_RIGHT to your preferred keycodes (see comment above variables.)
5. Optionally disable ALLOW_SLIDEOUTs (see comment above variables.)

Runs as root: requires touchpad's hidraw node, `/dev/uinput`, and an
exclusive grab of the touchpad.

Uninstall:

    sudo systemctl disable --now numberpadd
    sudo rm /usr/local/bin/numberpadd /etc/systemd/system/numberpadd.service

Troubleshooting: `sudo systemctl stop numberpadd`, then run
`sudo numberpadd --debug` in a terminal to see every touch, zone and key.
Timing, icon sizes, grid margins and brightness levels are constants at the top
of `numberpadd.py`.

## License

GPL-2.0-only.
